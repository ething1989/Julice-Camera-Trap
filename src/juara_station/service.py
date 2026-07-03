from __future__ import annotations

from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import date as date_type
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Event, Lock, Thread
import json
import logging
import math
import shutil
import shlex
import subprocess
import time
import wave

from .ai import (
    BirdNetAudioJob,
    BirdNetRunner,
    MockBirdNetRunner,
    birdnet_week,
)
from .audio import AudioRecorder, MockAudioRecorder
from .config import StationConfig, is_night
from .csv_exporter import CsvExportOptions, export_day_csv
from .paths import StationPaths, resolve_paths
from .sensors import MockSensorSuite, SensorSuite, read_cpu_temp
from .species_pack import write_active_species_list
from .storage import DataStore, SensorSample, from_iso, to_utc_iso, utc_now
from .timekeeper import TimeKeeper


LOGGER = logging.getLogger(__name__)
UTC = timezone.utc


class StationService:
    def __init__(self, config: StationConfig, paths: StationPaths, mock: bool = False, ai_only: bool = False):
        self.config = config
        self.paths = paths
        self.mock = mock
        self.ai_only = ai_only
        self.store = DataStore(paths.database_path)
        self.timekeeper = TimeKeeper(config.time, self.store)
        hardware_mock = mock or ai_only
        self.sensors = MockSensorSuite() if hardware_mock else SensorSuite(config.sensors)
        self.audio = MockAudioRecorder() if hardware_mock or not config.audio.enabled else AudioRecorder(config.audio)
        self.birdnet = MockBirdNetRunner() if mock else BirdNetRunner(config.birdnet, config.location)
        self._ai_lock = Lock()
        self._audio_worker_stop = Event()
        self._audio_worker_lock = Lock()
        self._audio_worker_thread: Thread | None = None
        self._coordinate_retry_lock = Lock()
        self._coordinate_retry_thread: Thread | None = None
        self._coordinate_retry_stop = Event()
        self._gps_coordinates_confirmed = False
        self._birdnet_prewarm_started = False
        self._reboot_scheduled_for: date_type | None = None
        self._startup_delay_done = False
        self._fallback_storage_since: datetime | None = None
        self._cooldown_active = False
        self._cooldown_high_count = 0
        self._cooldown_resume_count = 0
        self._cooldown_just_entered = False
        self._current_latitude = config.time.fallback_latitude
        self._current_longitude = config.time.fallback_longitude
        self._coordinate_source = "fallback"
        self._load_initial_coordinate_state()
        self._set_birdnet_location(self._current_latitude, self._current_longitude)

    def _load_initial_coordinate_state(self) -> None:
        previous = self._read_coordinate_state()
        if previous is None:
            return
        self._current_latitude, self._current_longitude = previous
        self._coordinate_source = "past"

    def _set_birdnet_location(self, latitude: float, longitude: float) -> None:
        if hasattr(self.birdnet, "set_location"):
            self.birdnet.set_location(latitude, longitude)

    def _uses_generated_birdnet_species_list(self) -> bool:
        mode = str(self.config.birdnet.species_filter_mode or "generated_list").strip().lower()
        if mode in {"generated_list", "generated", "custom_list", "custom"}:
            return True
        if mode in {"birdnet_location", "birdnet", "native", "location"}:
            return False
        LOGGER.warning("Unknown birdnet.species_filter_mode=%r; using generated_list behavior", mode)
        return True

    def run_forever(self) -> None:
        self._sleep_startup_delay_once()
        self.paths.ensure()
        self._clear_cooldown_marker()
        startup_days = self._record_startup_events()
        startup_days.update(self._prepare_dynamic_coordinates_and_species())
        startup_days.update(self._recover_audio_recording_state(utc_now()))
        self._export_changed_days(startup_days)
        self._ensure_coordinate_retry_worker()
        self._cleanup_stale_audio_files(utc_now())
        if (
            self.config.birdnet.enabled
            and self.config.birdnet.run_in_station_service
            and not self.config.birdnet.process_inline
            and not self.mock
        ):
            self._ensure_audio_worker()
            self._maybe_start_birdnet_prewarm()
        LOGGER.info("Juara station service started at %s", self.paths.root)
        try:
            while True:
                self.run_interval()
        finally:
            self._audio_worker_stop.set()
            self._coordinate_retry_stop.set()
            if self._audio_worker_thread:
                self._audio_worker_thread.join(timeout=2)
            if self._coordinate_retry_thread:
                self._coordinate_retry_thread.join(timeout=2)

    def _sleep_startup_delay_once(self) -> None:
        if self._startup_delay_done:
            return
        self._startup_delay_done = True
        delay = max(0, int(self.config.schedule.startup_delay_seconds))
        if delay <= 0 or self.mock or self.ai_only:
            return
        LOGGER.info("Waiting %s seconds before starting station hardware loops", delay)
        time.sleep(delay)

    def _maybe_switch_storage_root(self) -> None:
        if not self.paths.fallback_active:
            self._fallback_storage_since = None
            return
        try:
            refreshed = resolve_paths(self.config.storage)
        except Exception:
            return
        if refreshed.fallback_active:
            return
        if refreshed.database_path != self.paths.database_path:
            LOGGER.warning(
                "USB storage is available but live switch was skipped because database path would change: %s -> %s",
                self.paths.database_path,
                refreshed.database_path,
            )
            return
        self.paths = refreshed
        self._fallback_storage_since = None
        LOGGER.warning("USB storage became available; station outputs switched to %s", self.paths.root)

    def _record_time_source_errors(self, period_start: datetime, reading) -> None:
        if reading.source == "estimated":
            if self.config.time.gps_enabled:
                self.store.add_interval_error(period_start, "GPS Connection", source="time")
            self.store.add_interval_error(period_start, "RTC Connection", source="time")

    def _check_usb_missing_watchdog(self, now: datetime, period_start: datetime) -> None:
        if not self.paths.fallback_active:
            self._fallback_storage_since = None
            return
        self.store.add_interval_error(period_start, "USB Missing", source="storage")
        reboot_after = max(0, int(self.config.schedule.usb_missing_reboot_seconds))
        if reboot_after <= 0 or self.mock or self.ai_only:
            return
        if self._fallback_storage_since is None:
            self._fallback_storage_since = now
            return
        missing_seconds = (now - self._fallback_storage_since).total_seconds()
        if missing_seconds < reboot_after:
            return
        LOGGER.warning("USB has been missing for %.0fs; rebooting to recover mount", missing_seconds)
        self._request_reboot("USB Missing watchdog")

    def _request_reboot(self, reason: str) -> None:
        if self.mock or self.ai_only:
            LOGGER.info("Mock/AI-only mode skipping reboot requested by %s", reason)
            return
        command = shlex.split(self.config.schedule.cooldown_reboot_command)
        if not command:
            LOGGER.warning("%s requested a reboot but no reboot command is configured", reason)
            return
        try:
            subprocess.run(command, check=True, timeout=30)
        except Exception:
            LOGGER.exception("%s reboot command failed", reason)

    def run_interval(self, duration_seconds: int | None = None) -> Path:
        duration = duration_seconds or self.config.schedule.interval_seconds
        self._maybe_switch_storage_root()
        if self._cooldown_marker_exists():
            now = utc_now()
            period_start = floor_time(now, self.config.schedule.interval_seconds)
            return self._run_cooldown_interval(period_start, period_start + timedelta(seconds=duration), now, duration)
        reading = self.timekeeper.now(fallback_step=timedelta(seconds=self.config.schedule.interval_seconds))
        period_start = floor_time(reading.timestamp, self.config.schedule.interval_seconds)
        period_end = period_start + timedelta(seconds=self.config.schedule.interval_seconds)
        local_start = period_start.astimezone(self.config.zoneinfo)
        night = self._is_night(local_start)
        notes = "; ".join(filter(None, [reading.note, "fallback storage active" if self.paths.fallback_active else ""]))
        self._record_time_source_errors(period_start, reading)
        self._check_usb_missing_watchdog(reading.timestamp, period_start)
        changed_days = self._recover_audio_recording_state(reading.timestamp)
        self._cleanup_stale_audio_files(reading.timestamp)
        changed_days.update(self._purge_audio_backlog_if_due(reading.timestamp))
        self._ensure_coordinate_retry_worker()

        audio_result = None
        audio_paused_reason = self._audio_paused_reason(local_start)
        if audio_paused_reason:
            self._sample_until(period_end, duration)
            self.store.upsert_audio_event(
                period_start,
                "recording_paused",
                None,
                period_start,
                period_start,
                ai_status="done",
            )
            self.store.save_bird_calls(period_start, [])
            LOGGER.info("Skipping bird recording for %s: %s", local_start.isoformat(), audio_paused_reason)
        else:
            audio_path = self._audio_path(period_start)
            with ThreadPoolExecutor(max_workers=1) as executor:
                future = executor.submit(self.audio.record, audio_path, duration, night)
                self._sample_until(period_end, duration)
                audio_result = future.result()

            self.store.upsert_audio_event(
                period_start,
                audio_result.status,
                str(audio_result.path) if audio_result.path else None,
                audio_result.started_at,
                audio_result.ended_at,
                ai_status="done" if audio_result.status != "recorded" else None,
                error=audio_result.error,
            )
            if audio_result.status != "recorded":
                self.store.add_interval_error(
                    period_start,
                    "Recording Failed",
                    source="audio",
                    details=audio_result.error,
                )
                if _audio_error_is_microphone_connection(audio_result.error):
                    self.store.add_interval_error(
                        period_start,
                        "Microphone Connection",
                        source="audio",
                        details=audio_result.error,
                    )
                LOGGER.warning(
                    "Audio recording failed for %s: %s",
                    period_start.isoformat(),
                    audio_result.error or "unknown error",
                )
                if audio_result.path:
                    self._delete_audio_after_ai(audio_result.path)

            if self.config.birdnet.enabled and audio_result.status == "recorded" and audio_result.path:
                if self.mock or self.config.birdnet.process_inline:
                    self.process_audio_event(period_start, audio_result.path, night)
                elif self.config.birdnet.run_in_station_service:
                    self._ensure_audio_worker()

        self.store.upsert_interval_summary(period_start, period_end, reading.timestamp, reading.source, notes or None)
        if self._cooldown_just_entered:
            self.store.set_interval_system_event(period_start, "PI_COOLDOWN")
            self._cooldown_just_entered = False
        changed_days.add(period_start.astimezone(self.config.zoneinfo).date())

        exported = None
        for day in sorted(changed_days):
            exported = self._export_day(day)
        assert exported is not None
        return exported

    def _run_cooldown_interval(
        self,
        period_start: datetime,
        period_end: datetime,
        timestamp: datetime,
        duration_seconds: int,
    ) -> Path:
        self._sample_cpu_only_until(duration_seconds)
        self.store.upsert_audio_event(
            period_start,
            "recording_paused",
            None,
            period_start,
            period_start,
            ai_status="done",
        )
        self.store.save_bird_calls(period_start, [])
        self.store.upsert_interval_summary(period_start, period_end, timestamp, "system")
        self.store.set_interval_system_event(period_start, "PI_COOLDOWN")
        day = period_start.astimezone(self.config.zoneinfo).date()
        return self._export_day(day)

    def _export_changed_days(self, changed_days: set[date_type]) -> None:
        for day in sorted(changed_days):
            self._export_day(day)

    def _export_day(self, day: date_type) -> Path:
        output_path = self.paths.logs_dir / self.config.storage.csv_filename
        try:
            exported = export_day_csv(
                self.store,
                self.paths.logs_dir,
                datetime.combine(day, datetime.min.time(), tzinfo=self.config.zoneinfo),
                self.config.zoneinfo,
                options=self._csv_export_options(),
            )
            self._trigger_drive_sync(f"CSV export {exported.name}")
            return exported
        except Exception as exc:
            LOGGER.exception("CSV export failed for %s", day)
            self.store.add_interval_error(
                floor_time(utc_now(), self.config.schedule.interval_seconds),
                "CSV Write Failed",
                source="storage",
                details=str(exc),
            )
            return output_path

    def _csv_export_options(self) -> CsvExportOptions:
        return CsvExportOptions(
            filename=self.config.storage.csv_filename,
            profile=self.config.storage.csv_profile,
            latitude=self._current_latitude,
            longitude=self._current_longitude,
            interval_seconds=self.config.schedule.interval_seconds,
        )

    def _trigger_drive_sync(self, reason: str) -> None:
        if self.mock:
            return
        if not self.config.drive_sync.enabled or not self.config.drive_sync.trigger_on_csv_export:
            return
        command = shlex.split(self.config.drive_sync.trigger_command)
        if not command:
            return
        try:
            subprocess.run(command, check=False, timeout=10, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            LOGGER.info("Google Drive sync requested after %s", reason)
        except Exception:
            LOGGER.warning("Could not request Google Drive sync after %s", reason, exc_info=True)

    def _prepare_dynamic_coordinates_and_species(self, log_non_gps_event: bool = True) -> set[date_type]:
        changed_days: set[date_type] = set()
        if not self.config.time.coordinate_enabled:
            return changed_days

        try:
            reading = self.timekeeper.now(fallback_step=timedelta(seconds=0))
            now = reading.timestamp
            timestamp_source = reading.source
        except Exception:
            LOGGER.exception("Unable to timestamp coordinate selection; using system clock")
            now = utc_now()
            timestamp_source = "system"

        latitude, longitude, source, note = self._select_active_coordinates()
        self._current_latitude = latitude
        self._current_longitude = longitude
        self._coordinate_source = source
        self._gps_coordinates_confirmed = source == "gps"
        self._set_birdnet_location(latitude, longitude)

        event = {
            "gps": "GPS_COORDINATES",
            "past": "PAST_COORDINATES",
            "fallback": "FALLBACK_COORDINATES",
        }.get(source, "FALLBACK_COORDINATES")
        if source == "gps" or log_non_gps_event:
            changed_days.add(self._log_interval_event(now, event, timestamp_source))
        LOGGER.warning(
            "Coordinate source selected: %s lat=%.5f lon=%.5f%s",
            source,
            latitude,
            longitude,
            f" ({note})" if note else "",
        )

        pack_root = self.config.time.species_pack_root
        output_path = self.config.time.active_species_list_path
        if output_path is None and self.config.birdnet.species_list_path:
            output_path = Path(self.config.birdnet.species_list_path)
        if not self._uses_generated_birdnet_species_list():
            LOGGER.warning(
                "BirdNET native location filter enabled; active species list rebuild skipped for lat=%.5f lon=%.5f",
                latitude,
                longitude,
            )
            return changed_days
        if pack_root is None or output_path is None:
            return changed_days
        if source != "gps" and not log_non_gps_event:
            return changed_days

        try:
            selection = write_active_species_list(Path(pack_root), Path(output_path), latitude, longitude)
            LOGGER.warning(
                "Active BirdNET species list rebuilt from %s coordinates: %s species from %s nearest cells",
                source,
                selection.species_count,
                len(selection.cell_files),
            )
        except Exception as exc:
            LOGGER.exception("Dynamic BirdNET species-list rebuild failed")
            period_start = floor_time(now, self.config.schedule.interval_seconds)
            self.store.add_interval_error(period_start, "Species List Failed", source="birdnet", details=str(exc))
            changed_days.add(period_start.astimezone(self.config.zoneinfo).date())
        return changed_days

    def _ensure_coordinate_retry_worker(self) -> None:
        if self.mock or self.ai_only:
            return
        if not self.config.time.coordinate_enabled or self._gps_coordinates_confirmed:
            return
        with self._coordinate_retry_lock:
            if self._coordinate_retry_thread and self._coordinate_retry_thread.is_alive():
                return
            self._coordinate_retry_stop.clear()
            self._coordinate_retry_thread = Thread(target=self._run_coordinate_retry_worker, daemon=True)
            self._coordinate_retry_thread.start()

    def _run_coordinate_retry_worker(self) -> None:
        retry_sleep = max(60, int(self.config.time.coordinate_retry_seconds))
        while not self._coordinate_retry_stop.is_set() and not self._gps_coordinates_confirmed:
            try:
                changed_days = self._retry_dynamic_coordinates_and_species()
                if changed_days:
                    self._export_changed_days(changed_days)
                if self._gps_coordinates_confirmed:
                    return
            except Exception:
                LOGGER.exception("Background GPS coordinate retry failed")
            if self._coordinate_retry_stop.wait(retry_sleep):
                return

    def _retry_dynamic_coordinates_and_species(self) -> set[date_type]:
        if self._gps_coordinates_confirmed:
            return set()
        changed_days = self._prepare_dynamic_coordinates_and_species(log_non_gps_event=False)
        if self._gps_coordinates_confirmed:
            LOGGER.warning("GPS coordinates accepted after startup retry")
        return changed_days

    def _select_active_coordinates(self) -> tuple[float, float, str, str]:
        fallback = (self.config.time.fallback_latitude, self.config.time.fallback_longitude)
        wanted = max(1, int(self.config.time.coordinate_fix_count))
        fixes = self.timekeeper.read_gps_coordinates(wanted, self.config.time.coordinate_retry_seconds)
        if len(fixes) >= wanted:
            filtered = _filter_coordinate_fixes(fixes, self.config.time.coordinate_outlier_meters)
            minimum_consistent = _minimum_consistent_fix_count(
                wanted,
                self.config.time.coordinate_min_consistent_fraction,
            )
            if len(filtered) >= minimum_consistent:
                latitude = sum(fix.latitude for fix in filtered) / len(filtered)
                longitude = sum(fix.longitude for fix in filtered) / len(filtered)
                self._write_coordinate_state(latitude, longitude, "gps")
                return latitude, longitude, "gps", f"{len(filtered)}/{len(fixes)} consistent GPS fixes kept"
            if filtered:
                LOGGER.warning(
                    "GPS coordinates were not consistent enough; kept %s/%s fixes after outlier filtering, need %s",
                    len(filtered),
                    len(fixes),
                    minimum_consistent,
                )

        previous = self._read_coordinate_state()
        if previous is not None:
            latitude, longitude = previous
            return latitude, longitude, "past", "GPS unavailable; using last accepted field coordinates"

        self._write_coordinate_state(fallback[0], fallback[1], "fallback")
        return fallback[0], fallback[1], "fallback", "GPS unavailable; using backup deployment coordinates"

    def _coordinate_state_path(self) -> Path:
        return self.paths.state_dir / "active_coordinates.json"

    def _read_coordinate_state(self) -> tuple[float, float] | None:
        data = _read_json_file(self._coordinate_state_path())
        try:
            return float(data["latitude"]), float(data["longitude"])
        except (KeyError, TypeError, ValueError):
            return None

    def _write_coordinate_state(self, latitude: float, longitude: float, source: str) -> None:
        try:
            path = self._coordinate_state_path()
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                json.dumps(
                    {
                        "latitude": latitude,
                        "longitude": longitude,
                        "source": source,
                        "updated_at_utc": to_utc_iso(utc_now()),
                    },
                    indent=2,
                    sort_keys=True,
                )
                + "\n"
            )
        except OSError:
            LOGGER.warning("Unable to persist active coordinate state", exc_info=True)

    def _record_startup_events(self) -> set[date_type]:
        changed_days: set[date_type] = set()
        try:
            reading = self.timekeeper.now(fallback_step=timedelta(seconds=0))
        except Exception:
            LOGGER.exception("Unable to read startup timestamp; using system clock")
            reading = None
        now = reading.timestamp if reading else utc_now()
        timestamp_source = reading.source if reading else "system"
        state_path = self._startup_state_path()
        clean_marker = self._clean_shutdown_marker_path()
        previous = _read_json_file(state_path)
        current_boot_id = _current_boot_id()
        clean_shutdown = clean_marker.exists()

        events: list[str] = []
        previous_boot_id = previous.get("boot_id") if isinstance(previous, dict) else None
        if previous_boot_id and previous_boot_id != current_boot_id:
            events.append("PI_RESTARTED")
            if not clean_shutdown:
                events.append("POSSIBLE_POWER_LOSS_RECOVERY")
        elif previous and not clean_shutdown:
            events.append("UNEXPECTED_STATION_RESTART_RECOVERY")
        elif previous:
            events.append("STATION_SERVICE_RESTARTED")
        else:
            events.append("STATION_STARTED")

        for event in events:
            changed_days.add(self._log_interval_event(now, event, timestamp_source))
            LOGGER.warning("System event logged: %s", event)

        try:
            state_path.parent.mkdir(parents=True, exist_ok=True)
            state_path.write_text(
                json.dumps(
                    {
                        "boot_id": current_boot_id,
                        "started_at_utc": to_utc_iso(now),
                        "timestamp_source": timestamp_source,
                    },
                    indent=2,
                )
                + "\n"
            )
            clean_marker.unlink(missing_ok=True)
        except OSError:
            LOGGER.warning("Unable to update station startup state", exc_info=True)
        return changed_days

    def _log_interval_event(self, timestamp: datetime, event: str, timestamp_source: str = "system") -> date_type:
        period_start = floor_time(timestamp, self.config.schedule.interval_seconds)
        period_end = period_start + timedelta(seconds=self.config.schedule.interval_seconds)
        self.store.upsert_interval_event(period_start, period_end, timestamp, timestamp_source, event)
        return period_start.astimezone(self.config.zoneinfo).date()

    def _startup_state_path(self) -> Path:
        return self.paths.state_dir / "station_start_state.json"

    def _clean_shutdown_marker_path(self) -> Path:
        return self.paths.state_dir / "clean_shutdown.marker"

    def _write_clean_shutdown_marker(self) -> None:
        try:
            marker = self._clean_shutdown_marker_path()
            marker.parent.mkdir(parents=True, exist_ok=True)
            marker.write_text(to_utc_iso(utc_now()) + "\n")
        except OSError:
            LOGGER.warning("Unable to write clean shutdown marker", exc_info=True)

    def _cooldown_marker_path(self) -> Path:
        return self.paths.state_dir / "cpu_cooldown.active"

    def _cooldown_marker_exists(self) -> bool:
        if self.mock:
            return self._cooldown_active
        return self._cooldown_active or self._cooldown_marker_path().exists()

    def _write_cooldown_marker(self) -> None:
        try:
            marker = self._cooldown_marker_path()
            marker.parent.mkdir(parents=True, exist_ok=True)
            marker.write_text(to_utc_iso(utc_now()) + "\n")
        except OSError:
            LOGGER.warning("Unable to write CPU cooldown marker", exc_info=True)

    def _clear_cooldown_marker(self) -> None:
        try:
            self._cooldown_marker_path().unlink(missing_ok=True)
        except OSError:
            LOGGER.warning("Unable to clear CPU cooldown marker", exc_info=True)

    def _stop_ai_worker_for_cooldown(self) -> None:
        if self.mock or self.ai_only:
            return
        commands = [
            ["/usr/bin/sudo", "-n", "/usr/bin/systemctl", "stop", "juara-ai-worker.service"],
            ["/usr/bin/sudo", "-n", "/bin/systemctl", "stop", "juara-ai-worker.service"],
        ]
        for command in commands:
            if not Path(command[2]).exists():
                continue
            try:
                proc = subprocess.run(command, check=False, capture_output=True, text=True, timeout=15)
                if proc.returncode == 0:
                    LOGGER.warning("AI worker stopped immediately for CPU cooldown")
                    return
            except Exception:
                LOGGER.debug("AI worker cooldown stop command failed: %s", command, exc_info=True)
        LOGGER.warning("Unable to stop AI worker immediately for CPU cooldown; marker will stop the next cycle")

    def process_audio_event(self, period_start: datetime, audio_path: Path, night: bool) -> None:
        if not audio_path.exists():
            self._mark_missing_audio(period_start, audio_path)
            return
        output_dir = self.paths.ai_work_dir / "birdnet" / period_start.strftime("%Y%m%d_%H%M%S")
        try:
            self._set_birdnet_location(self._current_latitude, self._current_longitude)
            calls = self.birdnet.analyze_audio(audio_path, output_dir, period_start, night)
            self.store.save_bird_calls(period_start, calls)
            self.store.upsert_audio_event(period_start, "recorded", str(audio_path), ai_status="done")
            self._delete_audio_after_ai(audio_path)
        except Exception as exc:
            LOGGER.exception("BirdNET failed for %s", audio_path)
            self.store.upsert_audio_event(period_start, "recorded", str(audio_path), ai_status="retry", error=str(exc))

    def process_audio_backlog(self) -> set:
        return self.process_audio_backlog_rows(self.store.pending_audio_events())

    def run_ai_worker_forever(self, sleep_seconds: int = 60) -> None:
        self.paths.ensure()
        self._export_changed_days(self._recover_audio_recording_state(utc_now()))
        self._cleanup_stale_audio_files(utc_now())
        sleep_seconds = max(5, sleep_seconds)
        LOGGER.info("Juara AI backlog worker started at %s", self.paths.root)
        while True:
            if self._cooldown_marker_exists():
                LOGGER.warning("AI backlog worker is paused by CPU cooldown marker")
                time.sleep(sleep_seconds)
                continue
            try:
                self.run_ai_worker_once()
            except Exception:
                LOGGER.exception("AI backlog worker cycle failed")
            time.sleep(sleep_seconds)

    def run_ai_worker_once(self, now: datetime | None = None) -> set:
        now = now or utc_now()
        if self._cooldown_marker_exists():
            LOGGER.warning("Skipping AI worker cycle because CPU cooldown marker is active")
            return set()
        changed_days = set()
        changed_days.update(self._recover_audio_recording_state(now))
        self._cleanup_stale_audio_files(now)
        changed_days.update(self._purge_audio_backlog_if_due(now))
        if self.config.birdnet.enabled:
            rows = self.store.pending_audio_events()
            if rows and self._audio_batch_ready(rows, now=now):
                ready_rows = self._audio_rows_ready_for_processing(rows, now)
                if ready_rows:
                    changed_days.update(self.process_audio_backlog_rows(ready_rows))
                    self._maybe_schedule_post_audio_reboot(ready_rows, now)
        for day in sorted(changed_days):
            export_day_csv(
                self.store,
                self.paths.logs_dir,
                datetime.combine(day, datetime.min.time(), tzinfo=self.config.zoneinfo),
                self.config.zoneinfo,
                options=self._csv_export_options(),
            )
            self._trigger_drive_sync("AI worker CSV export")
        return changed_days

    def planned_reboot_cleanup(self, now: datetime | None = None) -> set[date_type]:
        now = now or utc_now()
        self.paths.ensure()
        changed_days = self._recover_audio_recording_state(
            now,
            interrupted_status="planned_reboot_partial",
            force_current_files=True,
            system_event="PARTIALLY_PROCESSED",
        )
        self._cleanup_stale_audio_files(now)
        self._write_clean_shutdown_marker()
        self._export_changed_days(changed_days)
        LOGGER.warning("Planned reboot cleanup finished; changed_days=%s", len(changed_days))
        return changed_days

    def process_audio_backlog_rows(self, rows) -> set:
        changed_days = set()
        rows = self._drop_missing_audio_rows(rows, changed_days)
        if not rows:
            return changed_days
        with self._ai_lock:
            if self._cooldown_marker_exists():
                LOGGER.warning("Stopping BirdNET backlog before processing because CPU cooldown marker is active")
                return changed_days
            for group in self._audio_backlog_groups(rows):
                if self._cooldown_marker_exists():
                    LOGGER.warning("Stopping BirdNET backlog before next batch because CPU cooldown marker is active")
                    break
                try:
                    self._set_birdnet_location(self._current_latitude, self._current_longitude)
                    batch_detections = self.birdnet.analyze_audio_batch(
                        [job for job, _row in group["jobs"]],
                        group["output_dir"],
                        group["week"],
                        group["night"],
                    )
                    for job, row in group["jobs"]:
                        calls = batch_detections.get(job.period_start, [])
                        self.store.save_bird_calls(job.period_start, calls)
                        self.store.upsert_audio_event(
                            job.period_start,
                            "recorded",
                            str(job.audio_path),
                            ai_status="done",
                            error=None,
                        )
                        self.store.refresh_interval_summary(job.period_start, self.config.schedule.interval_seconds)
                        self._delete_audio_after_ai(job.audio_path)
                        changed_days.add(job.period_start.astimezone(self.config.zoneinfo).date())
                except Exception as exc:
                    LOGGER.exception("BirdNET batch failed")
                    for job, row in group["jobs"]:
                        self.store.upsert_audio_event(
                            job.period_start,
                            "recorded",
                            str(job.audio_path),
                            ai_status="retry",
                            error=str(exc),
                        )
        return changed_days

    def _drop_missing_audio_rows(self, rows, changed_days: set) -> list:
        ready_rows = []
        for row in rows:
            audio_path = Path(row["path"] or "")
            period_start = from_iso(row["period_start_utc"])
            if row["path"] and audio_path.exists():
                ready_rows.append(row)
                continue
            self._mark_missing_audio(period_start, audio_path)
            self.store.refresh_interval_summary(period_start, self.config.schedule.interval_seconds)
            changed_days.add(period_start.astimezone(self.config.zoneinfo).date())
        return ready_rows

    def _mark_missing_audio(self, period_start: datetime, audio_path: Path) -> None:
        LOGGER.warning("Skipping missing audio recording for %s: %s", period_start.isoformat(), audio_path)
        self.store.save_bird_calls(period_start, [])
        self.store.upsert_audio_event(
            period_start,
            "missing_audio",
            str(audio_path) if str(audio_path) else None,
            ai_status="done",
            error=f"Missing audio recording: {audio_path}",
        )

    def _ensure_audio_worker(self) -> None:
        if self.config.birdnet.process_inline or not self.config.birdnet.enabled:
            return
        if not self.config.birdnet.run_in_station_service and not self.mock:
            return
        with self._audio_worker_lock:
            if self._audio_worker_thread and self._audio_worker_thread.is_alive():
                return
            self._audio_worker_stop.clear()
            self._audio_worker_thread = Thread(target=self._run_audio_backlog_worker, daemon=True)
            self._audio_worker_thread.start()

    def _maybe_start_birdnet_prewarm(self) -> None:
        if self.mock or not self.config.birdnet.enabled:
            return
        if not self.config.birdnet.prewarm_at_start:
            return
        if self.config.birdnet.use_subprocess or self.config.birdnet.python:
            return
        if self._birdnet_prewarm_started:
            return
        if self.store.pending_audio_events():
            return
        now = utc_now()
        self._birdnet_prewarm_started = True
        Thread(target=self._prewarm_birdnet, args=(now,), daemon=True).start()

    def _prewarm_birdnet(self, started_at: datetime) -> None:
        try:
            local = started_at.astimezone(self.config.zoneinfo)
            with self._ai_lock:
                self._set_birdnet_location(self._current_latitude, self._current_longitude)
                self.birdnet.prewarm(self.paths.ai_work_dir / "birdnet_prewarm", started_at, self._is_night(local))
            LOGGER.info("BirdNET prewarm finished")
        except Exception:
            LOGGER.exception("BirdNET prewarm failed; first real audio batch will retry normally")

    def _run_audio_backlog_worker(self) -> None:
        try:
            while not self._audio_worker_stop.is_set():
                changed_days = self._purge_audio_backlog_if_due(utc_now())
                for day in sorted(changed_days):
                    export_day_csv(
                        self.store,
                        self.paths.logs_dir,
                        datetime.combine(day, datetime.min.time(), tzinfo=self.config.zoneinfo),
                        self.config.zoneinfo,
                        options=self._csv_export_options(),
                    )
                    self._trigger_drive_sync("audio purge CSV export")
                rows = self.store.pending_audio_events()
                if not rows:
                    break
                now = utc_now()
                if not self._audio_batch_ready(rows, now=now):
                    if self._audio_worker_stop.wait(60):
                        break
                    continue
                ready_rows = self._audio_rows_ready_for_processing(rows, now)
                if not ready_rows:
                    if self._audio_worker_stop.wait(60):
                        break
                    continue
                changed_days = self.process_audio_backlog_rows(ready_rows)
                for day in sorted(changed_days):
                    export_day_csv(
                        self.store,
                        self.paths.logs_dir,
                        datetime.combine(day, datetime.min.time(), tzinfo=self.config.zoneinfo),
                        self.config.zoneinfo,
                        options=self._csv_export_options(),
                    )
                    self._trigger_drive_sync("audio backlog CSV export")
                self._maybe_schedule_post_audio_reboot(ready_rows, now)
        except Exception:
            LOGGER.exception("Audio AI backlog worker failed")

    def _recover_audio_recording_state(
        self,
        now: datetime,
        interrupted_status: str = "interrupted_power_loss",
        force_current_files: bool = False,
        system_event: str | None = None,
    ) -> set[date_type]:
        changed_days = self._recover_orphan_audio_recordings(
            now,
            interrupted_status=interrupted_status,
            force_current_files=force_current_files,
            system_event=system_event,
        )
        changed_days.update(
            self._recover_interrupted_audio_events(
                now,
                interrupted_status=interrupted_status,
                force_current_files=force_current_files,
                system_event=system_event,
            )
        )
        return changed_days

    def _recover_orphan_audio_recordings(
        self,
        now: datetime,
        interrupted_status: str = "interrupted_power_loss",
        force_current_files: bool = False,
        system_event: str | None = None,
    ) -> set[date_type]:
        changed_days: set[date_type] = set()
        if not self.paths.recordings_dir.exists():
            return changed_days
        current_file_grace_seconds = max(60, min(self.config.schedule.interval_seconds, 120))
        if force_current_files:
            current_file_grace_seconds = 0
        complete_threshold_seconds = max(1.0, self.config.schedule.interval_seconds * 0.90)
        for audio_path in sorted(self.paths.recordings_dir.glob("**/*.wav")):
            if self.store.audio_event_for_path(audio_path) is not None:
                continue
            try:
                age_seconds = now.timestamp() - audio_path.stat().st_mtime
            except OSError:
                continue
            if age_seconds < current_file_grace_seconds:
                continue
            period_start = self._period_start_from_audio_path(audio_path)
            if period_start is None:
                continue
            duration_seconds = _wav_duration_seconds(audio_path)
            if duration_seconds >= complete_threshold_seconds:
                ended_at = period_start + timedelta(seconds=duration_seconds)
                self.store.upsert_audio_event(
                    period_start,
                    "recorded",
                    str(audio_path),
                    period_start,
                    ended_at,
                    raw_json={"recovered_after_restart": True, "duration_seconds": duration_seconds},
                )
                self.store.refresh_interval_summary(period_start, self.config.schedule.interval_seconds)
                changed_days.add(period_start.astimezone(self.config.zoneinfo).date())
                LOGGER.warning(
                    "Recovered orphan audio recording after restart: %s duration=%.1fs",
                    audio_path,
                    duration_seconds,
                )
            else:
                self.store.upsert_audio_event(
                    period_start,
                    interrupted_status,
                    str(audio_path),
                    period_start,
                    now,
                    ai_status="done",
                    error=f"Audio recording interrupted before completion; duration {duration_seconds:.1f}s",
                )
                self.store.save_bird_calls(period_start, [])
                self.store.refresh_interval_summary(period_start, self.config.schedule.interval_seconds)
                if system_event:
                    self.store.set_interval_system_event(period_start, system_event)
                self._delete_audio_after_ai(audio_path)
                changed_days.add(period_start.astimezone(self.config.zoneinfo).date())
                LOGGER.warning(
                    "Deleted interrupted orphan audio recording after restart: %s duration=%.1fs",
                    audio_path,
                    duration_seconds,
                )
        return changed_days

    def _recover_interrupted_audio_events(
        self,
        now: datetime,
        interrupted_status: str = "interrupted_power_loss",
        force_current_files: bool = False,
        system_event: str | None = None,
    ) -> set[date_type]:
        changed_days: set[date_type] = set()
        current_file_grace_seconds = max(60, min(self.config.schedule.interval_seconds, 120))
        if force_current_files:
            current_file_grace_seconds = 0
        complete_threshold_seconds = max(1.0, self.config.schedule.interval_seconds * 0.90)
        for row in self.store.pending_audio_events():
            period_start = from_iso(row["period_start_utc"])
            audio_path = Path(row["path"] or "")
            if not row["path"] or not audio_path.exists():
                if (now - period_start).total_seconds() < self.config.schedule.interval_seconds + current_file_grace_seconds:
                    continue
                self._mark_missing_audio(period_start, audio_path)
                self.store.refresh_interval_summary(period_start, self.config.schedule.interval_seconds)
                changed_days.add(period_start.astimezone(self.config.zoneinfo).date())
                continue

            try:
                age_seconds = now.timestamp() - audio_path.stat().st_mtime
            except OSError:
                continue
            if age_seconds < current_file_grace_seconds:
                continue

            duration_seconds = _wav_duration_seconds(audio_path)
            if duration_seconds >= complete_threshold_seconds:
                continue

            self.store.upsert_audio_event(
                period_start,
                interrupted_status,
                str(audio_path),
                period_start,
                now,
                ai_status="done",
                error=f"Audio recording interrupted before completion; duration {duration_seconds:.1f}s",
            )
            self.store.save_bird_calls(period_start, [])
            self.store.refresh_interval_summary(period_start, self.config.schedule.interval_seconds)
            if system_event:
                self.store.set_interval_system_event(period_start, system_event)
            self._delete_audio_after_ai(audio_path)
            changed_days.add(period_start.astimezone(self.config.zoneinfo).date())
            LOGGER.warning(
                "Deleted interrupted pending audio recording after restart: %s duration=%.1fs",
                audio_path,
                duration_seconds,
            )
        return changed_days

    def _period_start_from_audio_path(self, audio_path: Path) -> datetime | None:
        try:
            local = datetime.strptime(audio_path.stem, "%Y%m%d_%H%M%S").replace(tzinfo=self.config.zoneinfo)
        except ValueError:
            return None
        return local.astimezone(UTC)

    def _cleanup_stale_audio_files(self, now: datetime) -> int:
        if not self.config.audio.delete_recordings_after_ai:
            return 0
        if not self.paths.recordings_dir.exists():
            return 0

        min_orphan_age_seconds = max(900, self.config.schedule.interval_seconds * 2)
        removed = 0
        for audio_path in sorted(self.paths.recordings_dir.glob("**/*.wav")):
            try:
                age_seconds = now.timestamp() - audio_path.stat().st_mtime
            except OSError:
                continue
            row = self.store.audio_event_for_path(audio_path)
            if row is None and age_seconds < min_orphan_age_seconds:
                continue
            if row is not None and row["status"] == "recorded" and row["ai_status"] in ("pending", "retry"):
                continue
            self._delete_audio_after_ai(audio_path)
            removed += 1

        for directory in sorted(self.paths.recordings_dir.glob("**/*"), reverse=True):
            if not directory.is_dir():
                continue
            try:
                directory.rmdir()
            except OSError:
                pass
        if removed:
            LOGGER.warning("Cleaned up %s stale internal audio recording(s)", removed)
        return removed

    def _purge_audio_backlog_if_due(self, now: datetime) -> set:
        local = now.astimezone(self.config.zoneinfo)
        purge_hour = self.config.schedule.audio_backlog_purge_hour % 24
        purge_minute = max(0, min(59, int(self.config.schedule.audio_backlog_purge_minute)))
        cutoff_local = datetime.combine(local.date(), datetime.min.time(), tzinfo=self.config.zoneinfo).replace(
            hour=purge_hour,
            minute=purge_minute,
        )
        if local < cutoff_local:
            return set()
        return self._purge_audio_backlog_before(cutoff_local.astimezone(UTC))

    def _purge_audio_backlog_before(self, cutoff: datetime) -> set:
        changed_days = set()
        rows = [row for row in self.store.pending_audio_events() if from_iso(row["period_start_utc"]) < cutoff]
        if not rows:
            return changed_days
        reason = "Unprocessed bird recording purged at the overnight AI catch-up cutoff"
        for row in rows:
            period_start = from_iso(row["period_start_utc"])
            audio_path = Path(row["path"] or "")
            if row["path"]:
                self._delete_audio_after_ai(audio_path)
            self.store.upsert_audio_event(
                period_start,
                "purged_at_3am",
                row["path"],
                ai_status="done",
                error=reason,
            )
            self.store.save_bird_calls(period_start, [])
            self.store.refresh_interval_summary(period_start, self.config.schedule.interval_seconds)
            changed_days.add(period_start.astimezone(self.config.zoneinfo).date())
        LOGGER.warning("Purged %s pending bird recording(s) older than %s", len(rows), cutoff.isoformat())
        return changed_days

    def _audio_batch_ready(self, rows, now: datetime | None = None) -> bool:
        if not rows:
            return False
        now = now or utc_now()
        if self.config.birdnet.night_batch_enabled:
            if any(self._audio_event_due_on_night_schedule(row, now) for row in rows):
                return True
            if self._is_night(now.astimezone(self.config.zoneinfo)):
                return False
        min_files = max(1, self.config.birdnet.batch_min_files)
        if len(rows) >= min_files:
            return True
        oldest = from_iso(rows[0]["period_start_utc"])
        max_wait = max(0, self.config.birdnet.batch_max_wait_seconds)
        return (now - oldest).total_seconds() >= max_wait

    def _audio_event_due_on_night_schedule(self, row, now: datetime) -> bool:
        period_start = from_iso(row["period_start_utc"])
        due_at = self._next_night_audio_flush_after(period_start)
        return due_at is not None and now.astimezone(self.config.zoneinfo) >= due_at

    def _delete_audio_after_ai(self, audio_path: Path) -> None:
        if not self.config.audio.delete_recordings_after_ai:
            return
        try:
            audio_path.unlink(missing_ok=True)
            audio_path.with_name(f"._{audio_path.name}").unlink(missing_ok=True)
        except OSError:
            LOGGER.warning("Unable to delete processed audio recording %s", audio_path, exc_info=True)

    def _audio_rows_ready_for_processing(self, rows, now: datetime):
        if not self.config.birdnet.night_batch_enabled:
            return rows
        due_rows = [row for row in rows if self._audio_event_due_on_night_schedule(row, now)]
        if due_rows:
            return due_rows
        if self._is_night(now.astimezone(self.config.zoneinfo)):
            return []
        return rows

    def _maybe_schedule_post_audio_reboot(self, rows, now: datetime) -> None:
        due_at = self._post_audio_reboot_due_at(rows, now)
        if due_at is None:
            return
        reboot_day = due_at.date()
        if self._reboot_scheduled_for == reboot_day:
            return
        self._reboot_scheduled_for = reboot_day
        self._request_reboot_after_delay(due_at)

    def _post_audio_reboot_due_at(self, rows, now: datetime) -> datetime | None:
        if not self.config.schedule.post_audio_reboot_enabled:
            return None
        now_local = now.astimezone(self.config.zoneinfo)
        candidates = []
        for row in rows:
            period_start = from_iso(row["period_start_utc"])
            due_at = self._next_night_audio_flush_after(period_start)
            if due_at is None:
                continue
            if due_at > now_local:
                continue
            if due_at.hour == self.config.schedule.post_audio_reboot_hour:
                candidates.append(due_at)
        return min(candidates) if candidates else None

    def _request_reboot_after_delay(self, due_at: datetime) -> None:
        delay_seconds = max(0, self.config.schedule.post_audio_reboot_delay_seconds)
        command = shlex.split(self.config.schedule.post_audio_reboot_command)
        if not command:
            LOGGER.warning("Post-audio reboot requested for %s but reboot command is empty", due_at.isoformat())
            return
        if self.mock:
            LOGGER.info("Mock mode skipping post-audio reboot for %s", due_at.isoformat())
            return
        LOGGER.warning(
            "%02d:00 audio bank finished and CSV exported; rebooting in %s seconds with: %s",
            due_at.hour,
            delay_seconds,
            " ".join(command),
        )
        if delay_seconds:
            time.sleep(delay_seconds)
        try:
            subprocess.run(command, check=True, timeout=30)
        except Exception:
            LOGGER.exception("Post-audio reboot command failed")

    def _next_night_audio_flush_after(self, value: datetime) -> datetime | None:
        interval = max(1, self.config.birdnet.night_batch_interval_seconds)
        local = value.astimezone(self.config.zoneinfo)
        local_day = local.date()
        for offset in (-1, 0, 1, 2):
            due = self._next_night_boundary_after(local, local_day + timedelta(days=offset), interval)
            if due is not None:
                return due
        return None

    def _next_night_boundary_after(
        self, local: datetime, window_start_day: date_type, interval_seconds: int
    ) -> datetime | None:
        zone = self.config.zoneinfo
        start = datetime.combine(window_start_day, datetime.min.time(), tzinfo=zone).replace(
            hour=self.config.schedule.night_start_hour
        )
        end_day = window_start_day
        if self.config.schedule.night_start_hour >= self.config.schedule.night_end_hour:
            end_day = window_start_day + timedelta(days=1)
        end = datetime.combine(end_day, datetime.min.time(), tzinfo=zone).replace(
            hour=self.config.schedule.night_end_hour
        )

        boundary = start
        step = timedelta(seconds=interval_seconds)
        while boundary <= end:
            if boundary > local:
                return boundary
            boundary += step
        return None

    def _audio_backlog_groups(self, rows):
        grouped = defaultdict(list)
        for row in rows:
            if not row["path"]:
                continue
            period_start = from_iso(row["period_start_utc"])
            local = period_start.astimezone(self.config.zoneinfo)
            key = (birdnet_week(period_start), self._is_night(local))
            grouped[key].append((BirdNetAudioJob(period_start, Path(row["path"])), row))

        max_files = max(1, self.config.birdnet.batch_max_files)
        for (week, night), jobs in grouped.items():
            for index in range(0, len(jobs), max_files):
                chunk = jobs[index : index + max_files]
                first_start = chunk[0][0].period_start
                last_start = chunk[-1][0].period_start
                suffix = "night" if night else "day"
                output_dir = (
                    self.paths.ai_work_dir
                    / "birdnet"
                    / f"batch_{first_start.strftime('%Y%m%d_%H%M%S')}_{last_start.strftime('%H%M%S')}_{suffix}"
                )
                yield {"week": week, "night": night, "jobs": chunk, "output_dir": output_dir}

    def _sample_until(self, period_end: datetime, duration_seconds: int) -> None:
        deadline = time.monotonic() + duration_seconds
        sample_every = max(1, self.config.schedule.sensor_sample_seconds)
        while True:
            try:
                sample = self.sensors.sample()
                self.store.insert_sensor_sample(sample)
                self._update_cooldown_counts(sample.cpu_temp_c)
                sample_period = floor_time(sample.sampled_at, self.config.schedule.interval_seconds)
                for error in sample.errors:
                    self.store.add_interval_error(sample_period, error, source="sensor")
            except Exception:
                LOGGER.exception("Sensor sample failed")
                self.store.add_interval_error(
                    floor_time(utc_now(), self.config.schedule.interval_seconds),
                    "Sensor Failed",
                    source="sensor",
                )
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            time.sleep(min(sample_every, remaining))

    def _sample_cpu_only_until(self, duration_seconds: int) -> None:
        deadline = time.monotonic() + duration_seconds
        sample_every = max(1, self.config.schedule.sensor_sample_seconds)
        while True:
            sampled_at = utc_now()
            try:
                cpu_temp = read_cpu_temp()
                self.store.insert_sensor_sample(
                    SensorSample(
                        sampled_at=sampled_at,
                        cpu_temp_c=cpu_temp,
                    )
                )
                self._update_cooldown_counts(cpu_temp)
            except Exception:
                LOGGER.exception("CPU temperature sample failed during cooldown")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            time.sleep(min(sample_every, remaining))

    def _update_cooldown_counts(self, cpu_temp_c: float | None) -> None:
        if cpu_temp_c is None:
            return
        high = self.config.schedule.cooldown_high_temp_c
        resume = self.config.schedule.cooldown_resume_temp_c
        needed = max(1, int(self.config.schedule.cooldown_consecutive_readings))
        if self._cooldown_active:
            if cpu_temp_c < resume:
                self._cooldown_resume_count += 1
            else:
                self._cooldown_resume_count = 0
            if self._cooldown_resume_count >= needed:
                LOGGER.warning(
                    "CPU cooled below %.1f C for %s readings; rebooting to resume nominal operation",
                    resume,
                    needed,
                )
                self._request_reboot("CPU cooldown complete")
            return

        if cpu_temp_c >= high:
            self._cooldown_high_count += 1
        else:
            self._cooldown_high_count = 0
        if self._cooldown_high_count >= needed:
            self._cooldown_active = True
            self._cooldown_just_entered = True
            self._cooldown_resume_count = 0
            self._write_cooldown_marker()
            self._stop_ai_worker_for_cooldown()
            LOGGER.warning("CPU cooldown mode entered after %s readings at or above %.1f C", needed, high)

    def _is_night(self, value: datetime) -> bool:
        return is_night(value.hour, self.config.schedule.night_start_hour, self.config.schedule.night_end_hour)

    def _audio_recording_disabled(self, local: datetime) -> bool:
        return is_night(
            local.hour,
            self.config.schedule.audio_recording_disabled_start_hour,
            self.config.schedule.audio_recording_disabled_end_hour,
        )

    def _audio_paused_reason(self, local: datetime) -> str | None:
        if self._audio_recording_disabled(local):
            return "overnight AI catch-up window"
        if self._sd_free_space_low():
            return "SD card free space below threshold"
        return None

    def _sd_free_space_low(self) -> bool:
        if self.mock or self.ai_only:
            return False
        threshold = float(self.config.schedule.sd_low_free_percent)
        if threshold <= 0:
            return False
        try:
            usage = shutil.disk_usage(self.paths.recordings_dir)
        except OSError:
            try:
                usage = shutil.disk_usage(self.paths.fallback_root)
            except OSError:
                return False
        if usage.total <= 0:
            return False
        free_percent = usage.free * 100.0 / usage.total
        if free_percent >= threshold:
            return False
        LOGGER.warning("Pausing audio recording because SD free space is %.1f%% below %.1f%%", free_percent, threshold)
        return True

    def _audio_path(self, period_start: datetime) -> Path:
        local = period_start.astimezone(self.config.zoneinfo)
        return self.paths.recordings_dir / local.strftime("%Y-%m-%d") / f"{local.strftime('%Y%m%d_%H%M%S')}.wav"


def floor_time(value: datetime, interval_seconds: int) -> datetime:
    value = value.astimezone(UTC)
    epoch = int(value.timestamp())
    floored = epoch - (epoch % interval_seconds)
    return datetime.fromtimestamp(floored, tz=UTC)


def _audio_error_is_microphone_connection(error: str | None) -> bool:
    if not error:
        return False
    text = error.lower()
    tokens = (
        "no such device",
        "cannot get card index",
        "audio open error",
        "unknown pcm",
        "device or resource busy",
        "no soundcards found",
    )
    return any(token in text for token in tokens)

def _filter_coordinate_fixes(fixes, outlier_meters: float) -> list:
    if len(fixes) < 3 or outlier_meters <= 0:
        return list(fixes)
    median_lat = _median([fix.latitude for fix in fixes])
    median_lon = _median([fix.longitude for fix in fixes])
    outlier_km = outlier_meters / 1000.0
    filtered = [
        fix
        for fix in fixes
        if _haversine_km(fix.latitude, fix.longitude, median_lat, median_lon) <= outlier_km
    ]
    return filtered


def _minimum_consistent_fix_count(wanted: int, fraction: float) -> int:
    wanted = max(1, int(wanted))
    try:
        fraction = float(fraction)
    except (TypeError, ValueError):
        fraction = 0.8
    fraction = max(0.0, min(1.0, fraction))
    return max(1, min(wanted, math.ceil(wanted * fraction)))


def _median(values: list[float]) -> float:
    ordered = sorted(values)
    midpoint = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[midpoint]
    return (ordered[midpoint - 1] + ordered[midpoint]) / 2.0


def _haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    radius_km = 6371.0088
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    return 2 * radius_km * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def _read_json_file(path: Path) -> dict:
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def _current_boot_id() -> str:
    try:
        return Path("/proc/sys/kernel/random/boot_id").read_text().strip()
    except OSError:
        return "unknown"


def _wav_duration_seconds(path: Path) -> float:
    try:
        with wave.open(str(path), "rb") as handle:
            frame_rate = handle.getframerate()
            channels = handle.getnchannels()
            sample_width = handle.getsampwidth()
            if frame_rate <= 0 or channels <= 0 or sample_width <= 0:
                return 0.0
            header_duration = float(handle.getnframes()) / float(frame_rate)
            data_bytes_on_disk = max(0, path.stat().st_size - 44)
            byte_rate = frame_rate * channels * sample_width
            disk_duration = data_bytes_on_disk / float(byte_rate)
            return min(header_duration, disk_duration)
    except (OSError, EOFError, wave.Error):
        return 0.0
