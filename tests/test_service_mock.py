from dataclasses import replace
from pathlib import Path
import csv

from juara_station.config import (
    AudioConfig,
    BirdNetConfig,
    DriveSyncConfig,
    ScheduleConfig,
    StationConfig,
    StorageConfig,
    TimeConfig,
)
from juara_station.paths import resolve_paths
from juara_station.service import StationService


def _mock_config(tmp_path: Path, **overrides) -> StationConfig:
    config = StationConfig(
        storage=StorageConfig(
            root=tmp_path / "usb",
            fallback_root=tmp_path / "fallback",
            state_root=tmp_path / "state",
            work_root=tmp_path / "work",
            recording_root=tmp_path / "recordings",
            logs_subdir=".",
            csv_filename="Julice Camera Trap July 2026.csv",
            csv_profile="julice_camera_trap",
        ),
        schedule=ScheduleConfig(interval_seconds=1, sensor_sample_seconds=1, startup_delay_seconds=0),
        time=TimeConfig(gps_enabled=False, rtc_read_command="/bin/false", rtc_write_enabled=False, coordinate_enabled=False),
        audio=AudioConfig(delete_recordings_after_ai=True),
        birdnet=BirdNetConfig(enabled=True, process_inline=False, batch_max_files=1),
        drive_sync=DriveSyncConfig(enabled=False),
    )
    return replace(config, **overrides) if overrides else config


def test_mock_interval_creates_csv_and_deletes_audio_without_media_folders(tmp_path: Path):
    config = _mock_config(tmp_path)
    paths = resolve_paths(config.storage)
    service = StationService(config, paths, mock=True)

    csv_path = service.run_interval(duration_seconds=1)

    assert csv_path == tmp_path / "usb" / "Julice Camera Trap July 2026.csv"
    rows = list(csv.DictReader(csv_path.open()))
    assert rows
    assert "Call 1" in rows[0]
    assert "photos_taken" not in rows[0]
    assert not list((tmp_path / "recordings").glob("**/*.wav"))
    assert not (tmp_path / "usb" / "media" / "audio").exists()
    assert not (tmp_path / "usb" / "Photos").exists()


def test_drive_sync_trigger_is_nonblocking_and_uses_configured_command(tmp_path: Path, monkeypatch):
    calls = []

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))

    monkeypatch.setattr("juara_station.service.subprocess.run", fake_run)
    config = _mock_config(
        tmp_path,
        drive_sync=DriveSyncConfig(
            enabled=True,
            trigger_on_csv_export=True,
            trigger_command="/usr/bin/systemctl start --no-block juara-gdrive-sync.service",
        ),
    )
    service = StationService(config, resolve_paths(config.storage), mock=False, ai_only=True)

    service._trigger_drive_sync("test export")

    assert calls[0][0] == ["/usr/bin/systemctl", "start", "--no-block", "juara-gdrive-sync.service"]
    assert calls[0][1]["timeout"] == 10
