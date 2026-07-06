from __future__ import annotations

from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.9/3.10 local testing.
    import tomli as tomllib


@dataclass(frozen=True)
class LocationConfig:
    latitude: float = -17.102778
    longitude: float = -56.941639
    country: str = "BRA"
    admin1_region: str = "BR-MT"
    timezone: str = "America/Cuiaba"


@dataclass(frozen=True)
class StorageConfig:
    root: Path = Path("/var/lib/juara-station/local")
    fallback_root: Path = Path("/var/lib/juara-station")
    state_root: Path | None = None
    work_root: Path | None = None
    recording_root: Path | None = None
    logs_subdir: str = "."
    require_usb: bool = False
    csv_filename: str = "Julice Camera Trap July 2026.csv"
    csv_profile: str = "julice_camera_trap"


@dataclass(frozen=True)
class ScheduleConfig:
    interval_seconds: int = 300
    sensor_sample_seconds: int = 60
    night_start_hour: int = 18
    night_end_hour: int = 6
    audio_recording_disabled_start_hour: int = 1
    audio_recording_disabled_end_hour: int = 3
    audio_backlog_purge_hour: int = 3
    audio_backlog_purge_minute: int = 0
    post_audio_reboot_enabled: bool = False
    post_audio_reboot_hour: int = 3
    post_audio_reboot_delay_seconds: int = 180
    post_audio_reboot_command: str = "/usr/bin/sudo -n /usr/sbin/reboot"
    startup_delay_seconds: int = 0
    usb_missing_reboot_seconds: int = 0
    cooldown_high_temp_c: float = 75.0
    cooldown_resume_temp_c: float = 70.0
    cooldown_consecutive_readings: int = 10
    cooldown_reboot_command: str = "/usr/bin/sudo -n /usr/sbin/reboot"
    sd_low_free_percent: float = 10.0


@dataclass(frozen=True)
class AudioModeConfig:
    sample_rate: int
    sample_format: str
    channels: int = 1


@dataclass(frozen=True)
class AudioConfig:
    enabled: bool = True
    device: str = "default"
    day: AudioModeConfig = field(default_factory=lambda: AudioModeConfig(sample_rate=48000, sample_format="S32_LE"))
    night: AudioModeConfig = field(default_factory=lambda: AudioModeConfig(sample_rate=24000, sample_format="S16_LE"))
    record_command: str = "arecord"
    mixer_command: str = "amixer"
    capture_gain_percent: int | None = 100
    capture_gain_controls: list[str] = field(default_factory=lambda: ["Mic", "Capture"])
    delete_recordings_after_ai: bool = True
    segment_seconds: int = 0


@dataclass(frozen=True)
class SensorConfig:
    enabled: bool = True
    bme280_address: int = 0x76
    veml7700_address: int = 0x10
    stagger_read_seconds: float = 0.0
    scd41_enabled: bool = False
    scd41_address: int = 0x62
    uart_co2_enabled: bool = False
    uart_co2_rx_gpio: int = 23
    uart_co2_tx_gpio: int = 24
    uart_co2_baudrate: int = 9600
    uart_co2_warmup_seconds: int = 30
    pms5003_enabled: bool = False
    pms5003_device: str = "/dev/ttyAMA0"
    pms5003_baudrate: int = 9600
    pms5003_warmup_seconds: int = 10


@dataclass(frozen=True)
class TimeConfig:
    gps_enabled: bool = True
    gps_command: str = "gpspipe"
    rtc_read_command: str = "/usr/sbin/hwclock"
    rtc_write_enabled: bool = True
    large_drift_minutes: float = 5.0
    small_drift_minutes: float = 1.0
    gps_large_drift_sync_count: int = 3
    coordinate_enabled: bool = True
    coordinate_fix_count: int = 10
    coordinate_retry_seconds: int = 30
    coordinate_outlier_meters: float = 300.0
    coordinate_min_consistent_fraction: float = 0.8
    coordinate_max_distance_from_fallback_km: float = 0.0
    fallback_latitude: float = -16.68260
    fallback_longitude: float = -56.90453
    internet_coordinate_enabled: bool = True
    internet_coordinate_timeout_seconds: float = 4.0
    internet_coordinate_urls: list[str] = field(default_factory=list)
    species_area_radius_km: float = 100.0
    species_pack_root: Path | None = None
    active_species_list_path: Path | None = None


@dataclass(frozen=True)
class BirdNetConfig:
    enabled: bool = True
    backend: str = "birdnet"
    python: str | None = None
    species_filter_mode: str = "generated_list"
    species_list_path: str | None = None
    run_in_station_service: bool = False
    use_subprocess: bool = False
    prewarm_at_start: bool = True
    process_inline: bool = False
    batch_min_files: int = 1
    batch_max_files: int = 48
    batch_max_wait_seconds: int = 3600
    night_batch_enabled: bool = False
    night_batch_interval_seconds: int = 14400
    keep_work_outputs: bool = False
    ffmpeg_command: str = "ffmpeg"
    audio_gain_db: float = 36.0
    min_confidence: float = 0.25
    candidate_min_confidence: float = 0.10
    sensitivity_day: float = 1.0
    sensitivity_night: float = 0.8
    overlap_day: float = 1.5
    overlap_night: float = 0.0
    workers: int = 1
    batch_size: int = 1
    fast_tflite: bool = False
    sf_threshold: float = 0.005


@dataclass(frozen=True)
class YamNetConfig:
    enabled: bool = False
    python: str | None = None
    model_path: str | None = None
    class_map_path: str | None = None
    ffmpeg_command: str = "ffmpeg"
    min_confidence: float = 0.15
    top_k: int = 8
    max_audio_seconds: int = 30
    subprocess_timeout_seconds: int = 600


@dataclass(frozen=True)
class DriveSyncConfig:
    enabled: bool = True
    trigger_on_csv_export: bool = True
    trigger_command: str = "/usr/bin/systemctl start --no-block juara-gdrive-sync.service"


@dataclass(frozen=True)
class StationConfig:
    location: LocationConfig = field(default_factory=LocationConfig)
    storage: StorageConfig = field(default_factory=StorageConfig)
    schedule: ScheduleConfig = field(default_factory=ScheduleConfig)
    audio: AudioConfig = field(default_factory=AudioConfig)
    sensors: SensorConfig = field(default_factory=SensorConfig)
    time: TimeConfig = field(default_factory=TimeConfig)
    birdnet: BirdNetConfig = field(default_factory=BirdNetConfig)
    yamnet: YamNetConfig = field(default_factory=YamNetConfig)
    drive_sync: DriveSyncConfig = field(default_factory=DriveSyncConfig)

    @property
    def zoneinfo(self) -> ZoneInfo:
        return ZoneInfo(self.location.timezone)


def load_config(path: str | Path | None = None) -> StationConfig:
    config = StationConfig()
    if path is None:
        return config
    data = tomllib.loads(Path(path).read_text())
    return _merge_dataclass(config, data)


def _merge_dataclass(instance: Any, data: dict[str, Any]) -> Any:
    values: dict[str, Any] = {}
    fields = getattr(instance, "__dataclass_fields__", {})
    for key, value in data.items():
        if key not in fields:
            raise ValueError(f"Unknown config key: {key}")
        current = getattr(instance, key)
        if hasattr(current, "__dataclass_fields__") and isinstance(value, dict):
            values[key] = _merge_dataclass(current, value)
        elif isinstance(current, Path) or (key.endswith("_root") and isinstance(value, str)) or (
            key.endswith("_path") and isinstance(value, str)
        ):
            values[key] = Path(value)
        else:
            values[key] = value
    return replace(instance, **values)


def is_night(local_hour: int, start_hour: int, end_hour: int) -> bool:
    if start_hour == end_hour:
        return True
    if start_hour < end_hour:
        return start_hour <= local_hour < end_hour
    return local_hour >= start_hour or local_hour < end_hour
