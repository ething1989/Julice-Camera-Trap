from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import json
import select
import shlex
import subprocess
import time

from .config import TimeConfig
from .storage import DataStore, from_iso, utc_now


UTC = timezone.utc


@dataclass(frozen=True)
class TimeReading:
    timestamp: datetime
    source: str
    note: str = ""


@dataclass(frozen=True)
class CoordinateFix:
    latitude: float
    longitude: float
    timestamp: datetime | None = None


class TimeKeeper:
    def __init__(self, config: TimeConfig, store: DataStore):
        self.config = config
        self.store = store

    def now(self, fallback_step: timedelta = timedelta(minutes=5)) -> TimeReading:
        gps = self._read_gps_time()
        rtc = self._read_rtc_time()
        state = self.store.get_time_state()
        bad_count = int(state["bad_gps_count"])

        if gps and rtc:
            drift = abs((gps - rtc).total_seconds()) / 60
            if drift >= self.config.large_drift_minutes:
                bad_count += 1
                if bad_count >= self.config.gps_large_drift_sync_count:
                    self._write_rtc_time(gps)
                    self.store.update_time_state(gps, 0)
                    return TimeReading(gps, "gps_rtc_resync", f"GPS/RTC drift {drift:.1f} min; RTC resynced")
                self.store.update_time_state(rtc, bad_count)
                return TimeReading(rtc, "rtc", f"GPS ignored; GPS/RTC drift {drift:.1f} min")
            if drift >= self.config.small_drift_minutes:
                self._write_rtc_time(gps)
                self.store.update_time_state(gps, 0)
                return TimeReading(gps, "gps_rtc_corrected", f"RTC corrected; drift {drift:.1f} min")
            self.store.update_time_state(gps, 0)
            return TimeReading(gps, "gps")

        if gps:
            self.store.update_time_state(gps, 0)
            return TimeReading(gps, "gps")

        if rtc:
            self.store.update_time_state(rtc, bad_count)
            return TimeReading(rtc, "rtc")

        last = state["last_timestamp_utc"]
        fallback = (from_iso(last) + fallback_step) if last else utc_now()
        self.store.update_time_state(fallback, bad_count)
        return TimeReading(fallback, "estimated", "GPS and RTC unavailable")

    def _read_gps_time(self) -> datetime | None:
        if not self.config.gps_enabled:
            return None
        try:
            proc = subprocess.run(
                [self.config.gps_command, "-w", "-n", "10"],
                check=False,
                capture_output=True,
                text=True,
                timeout=8,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        if proc.returncode not in (0, 124):
            return None
        for line in proc.stdout.splitlines():
            try:
                packet = json.loads(line)
            except json.JSONDecodeError:
                continue
            if packet.get("class") != "TPV" or "time" not in packet:
                continue
            if packet.get("mode", 0) < 2:
                continue
            try:
                return datetime.fromisoformat(packet["time"].replace("Z", "+00:00")).astimezone(UTC)
            except ValueError:
                continue
        return None

    def read_gps_coordinates(self, fix_count: int | None = None, timeout_seconds: int | None = None) -> list[CoordinateFix]:
        if not self.config.gps_enabled:
            return []
        wanted = max(1, int(fix_count or self.config.coordinate_fix_count))
        timeout = max(5, int(timeout_seconds or self.config.coordinate_retry_seconds))
        try:
            proc = subprocess.Popen(
                [self.config.gps_command, "-w"],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
            )
        except OSError:
            return []

        if proc.stdout is None:
            _terminate_process(proc)
            return []

        fixes: list[CoordinateFix] = []
        deadline = time.monotonic() + timeout
        try:
            while len(fixes) < wanted and time.monotonic() < deadline:
                remaining = max(0.0, deadline - time.monotonic())
                ready, _, _ = select.select([proc.stdout], [], [], min(1.0, remaining))
                if not ready:
                    if proc.poll() is not None:
                        break
                    continue
                line = proc.stdout.readline()
                if not line:
                    if proc.poll() is not None:
                        break
                    continue
                fix = _coordinate_fix_from_gps_packet(line)
                if fix is not None:
                    fixes.append(fix)
            return fixes
        finally:
            _terminate_process(proc)

    def _read_rtc_time(self) -> datetime | None:
        base_command = shlex.split(self.config.rtc_read_command)
        commands = [
            [*base_command, "--show", "--utc"],
            [*base_command, "-r", "-u"],
        ]
        for command in commands:
            try:
                proc = subprocess.run(command, check=False, capture_output=True, text=True, timeout=5)
            except (OSError, subprocess.SubprocessError):
                continue
            if proc.returncode != 0:
                continue
            parsed = _parse_hwclock_output(proc.stdout.strip())
            if parsed:
                return parsed
        return None

    def _write_rtc_time(self, timestamp: datetime) -> None:
        if not self.config.rtc_write_enabled:
            return
        stamp = timestamp.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S")
        base_command = shlex.split(self.config.rtc_read_command)
        try:
            subprocess.run(
                [*base_command, "--set", "--date", stamp, "--utc"],
                check=False,
                capture_output=True,
                text=True,
                timeout=10,
            )
        except (OSError, subprocess.SubprocessError):
            return


def _parse_hwclock_output(value: str) -> datetime | None:
    candidates = [value.splitlines()[0].strip()] if value else []
    for candidate in candidates:
        candidate = candidate.replace("  ", " ")
        if candidate.endswith("Z"):
            candidate = candidate[:-1] + "+00:00"
        try:
            parsed = datetime.fromisoformat(candidate)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=UTC)
            return parsed.astimezone(UTC)
        except ValueError:
            pass
        for fmt in ("%Y-%m-%d %H:%M:%S.%f%z", "%Y-%m-%d %H:%M:%S%z", "%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S"):
            try:
                parsed = datetime.strptime(candidate, fmt)
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=UTC)
                return parsed.astimezone(UTC)
            except ValueError:
                continue
    return None


def _coordinate_fix_from_gps_packet(line: str) -> CoordinateFix | None:
    try:
        packet = json.loads(line)
    except json.JSONDecodeError:
        return None
    if packet.get("class") != "TPV" or packet.get("mode", 0) < 2:
        return None
    if "lat" not in packet or "lon" not in packet:
        return None
    try:
        latitude = float(packet["lat"])
        longitude = float(packet["lon"])
    except (TypeError, ValueError):
        return None
    timestamp = None
    if packet.get("time"):
        try:
            timestamp = datetime.fromisoformat(str(packet["time"]).replace("Z", "+00:00")).astimezone(UTC)
        except ValueError:
            timestamp = None
    return CoordinateFix(latitude, longitude, timestamp)


def _terminate_process(proc: subprocess.Popen) -> None:
    if proc.poll() is not None:
        return
    proc.terminate()
    try:
        proc.wait(timeout=2)
    except subprocess.SubprocessError:
        proc.kill()
