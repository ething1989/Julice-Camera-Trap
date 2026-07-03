from __future__ import annotations

from datetime import datetime
from datetime import timedelta
from dataclasses import dataclass
from io import StringIO
from pathlib import Path
from zoneinfo import ZoneInfo
import csv

from .paths import atomic_replace_text
from .storage import DataStore, from_iso


MMHG_PER_INHG = 25.4
MAX_BIRD_CALL_COLUMNS = 90
CALL_COLUMNS = [f"Call {index}" for index in range(1, MAX_BIRD_CALL_COLUMNS + 1)]


@dataclass(frozen=True)
class CsvExportOptions:
    filename: str = "juara_station.csv"
    profile: str = "standard"
    latitude: float | None = None
    longitude: float | None = None
    interval_seconds: int = 300

CSV_COLUMNS = [
    "timestamp",
    "timestamp_source",
    "system_event",
    "temperature_c_avg",
    "humidity_pct_avg",
    "pressure_inhg_avg",
    "lux_avg",
    "co2_ppm_avg",
    "pm1_0_ug_m3_avg",
    "pm2_5_ug_m3_avg",
    "pm10_ug_m3_avg",
    "particles_0_3_per_l_avg",
    "particles_0_5_per_l_avg",
    "cpu_temp_c_avg",
    "bird_species_richness",
    "bird_total_calls",
    "bird_total_species",
    "bird_top_species",
    "bird_shannon_index",
    "bird_simpson_index",
    "bird_pielou_evenness",
    "audio_status",
    "bird_calls_truncated",
    *CALL_COLUMNS,
]

JULICE_CAMERA_TRAP_COLUMNS = [
    "Timestamp",
    "Time_Source",
    "Pi_Event",
    "Temp",
    "Humidity",
    "Lux",
    "mmHg",
    "Pi_cpu_temp",
    "lat",
    "lon",
    "species_richness",
    "total_calls",
    "total_species",
    "top_species",
    "shannon_index",
    "simpsons_index",
    "pielou_evenness",
    "Audio_status",
    *CALL_COLUMNS,
    "",
    "Errors",
]


def export_day_csv(
    store: DataStore,
    logs_dir: Path,
    local_day: datetime,
    zone: ZoneInfo,
    options: CsvExportOptions | None = None,
) -> Path:
    return export_main_csv(store, logs_dir, zone, options=options)


def export_main_csv(
    store: DataStore,
    logs_dir: Path,
    zone: ZoneInfo,
    options: CsvExportOptions | None = None,
) -> Path:
    options = options or CsvExportOptions()
    rows = _coalesce_event_only_rows(store.list_intervals(), options.interval_seconds)
    call_rows_by_interval = _bird_call_rows_by_interval(store)
    errors_by_interval = _errors_by_interval(store)
    julice_profile = options.profile == "julice_camera_trap"
    columns = list(JULICE_CAMERA_TRAP_COLUMNS if julice_profile else CSV_COLUMNS)
    output = StringIO()
    writer = csv.DictWriter(output, fieldnames=columns, extrasaction="ignore")
    writer.writeheader()
    for row in rows:
        bird_calls = call_rows_by_interval.get(row["period_start_utc"])
        interval_errors = errors_by_interval.get(row["period_start_utc"], [])
        if julice_profile:
            writer.writerow(_row_to_julice_csv(row, zone, bird_calls, interval_errors, options))
        else:
            writer.writerow(_row_to_csv(row, zone, bird_calls))
    path = logs_dir / options.filename
    atomic_replace_text(path, output.getvalue())
    for old_path in logs_dir.glob("*_juara_station.csv"):
        old_path.unlink(missing_ok=True)
    _remove_bird_calls_csv(logs_dir)
    return path


def _remove_bird_calls_csv(logs_dir: Path) -> None:
    (logs_dir / "juara_bird_calls.csv").unlink(missing_ok=True)
    for old_path in logs_dir.glob("*_juara_bird_calls.csv"):
        old_path.unlink(missing_ok=True)


def _coalesce_event_only_rows(rows, interval_seconds: int) -> list[dict]:
    interval = max(1, int(interval_seconds))
    by_key: dict[str, dict] = {}
    for row in rows:
        item = dict(row)
        key = item["period_start_utc"]
        if _is_event_only_row(item):
            floor_key = _floor_interval_key(from_iso(key), interval)
            if floor_key != key:
                item["period_start_utc"] = floor_key
                item["period_end_utc"] = _iso_seconds(from_iso(floor_key) + timedelta(seconds=interval))
                existing = by_key.get(floor_key)
                if existing is None:
                    by_key[floor_key] = item
                else:
                    existing["system_event"] = _append_csv_events(existing.get("system_event"), item.get("system_event"))
                continue
        existing = by_key.get(key)
        if existing is not None and _is_event_only_row(existing):
            item["system_event"] = _append_csv_events(existing.get("system_event"), item.get("system_event"))
        elif existing is not None:
            existing["system_event"] = _append_csv_events(existing.get("system_event"), item.get("system_event"))
            continue
        by_key[key] = item
    return [by_key[key] for key in sorted(by_key)]


def _is_event_only_row(row: dict) -> bool:
    if not row.get("system_event"):
        return False
    empty_fields = (
        "temperature_c_avg",
        "humidity_pct_avg",
        "pressure_mmhg_avg",
        "lux_avg",
        "co2_ppm_avg",
        "pm1_0_ug_m3_avg",
        "pm2_5_ug_m3_avg",
        "pm10_ug_m3_avg",
        "particles_0_3_per_l_avg",
        "particles_0_5_per_l_avg",
        "cpu_temp_c_avg",
        "bird_summary",
        "bird_species_richness",
        "bird_total_calls",
        "bird_total_species",
        "bird_top_species",
        "bird_shannon_index",
        "bird_simpson_index",
        "bird_pielou_evenness",
        "bird_call_cells",
        "audio_path",
        "notes",
    )
    if any(row.get(field) not in (None, "") for field in empty_fields):
        return False
    if row.get("audio_status") not in (None, ""):
        return False
    return True


def _floor_interval_key(value: datetime, interval_seconds: int) -> str:
    epoch = int(value.timestamp())
    floored = epoch - (epoch % interval_seconds)
    return _iso_seconds(datetime.fromtimestamp(floored, tz=value.tzinfo))


def _iso_seconds(value: datetime) -> str:
    return value.isoformat(timespec="seconds")


def _append_csv_events(existing: str | None, event: str | None) -> str:
    parts = [part.strip() for part in (existing or "").replace("\n", ";").split(";") if part.strip()]
    for part in (event or "").replace("\n", ";").split(";"):
        value = part.strip()
        if value and value not in parts:
            parts.append(value)
    return ";".join(parts)


def _row_to_csv(row, zone: ZoneInfo, bird_calls: list[dict] | None = None) -> dict[str, str | int | float | None]:
    timestamp = from_iso(row["timestamp_utc"]).astimezone(zone).strftime("%Y-%m-%dT%H:%M:%S")
    selected_calls, truncated = _selected_call_cells(bird_calls or [])
    output = {
        "timestamp": timestamp,
        "timestamp_source": row["timestamp_source"],
        "system_event": row["system_event"] or "",
        "temperature_c_avg": _round(row["temperature_c_avg"]),
        "humidity_pct_avg": _round(row["humidity_pct_avg"]),
        "pressure_inhg_avg": _round(_mmhg_to_inhg(row["pressure_mmhg_avg"])),
        "lux_avg": _round(row["lux_avg"]),
        "co2_ppm_avg": _round(row["co2_ppm_avg"]),
        "pm1_0_ug_m3_avg": _round(row["pm1_0_ug_m3_avg"]),
        "pm2_5_ug_m3_avg": _round(row["pm2_5_ug_m3_avg"]),
        "pm10_ug_m3_avg": _round(row["pm10_ug_m3_avg"]),
        "particles_0_3_per_l_avg": _round(row["particles_0_3_per_l_avg"]),
        "particles_0_5_per_l_avg": _round(row["particles_0_5_per_l_avg"]),
        "cpu_temp_c_avg": _round(row["cpu_temp_c_avg"]),
        "bird_species_richness": row["bird_species_richness"] or "",
        "bird_total_calls": row["bird_total_calls"] or "",
        "bird_total_species": row["bird_total_species"] or "",
        "bird_top_species": row["bird_top_species"] or "",
        "bird_shannon_index": _round(row["bird_shannon_index"]),
        "bird_simpson_index": _round(row["bird_simpson_index"]),
        "bird_pielou_evenness": _round(row["bird_pielou_evenness"]),
        "audio_status": row["audio_status"] or "",
        "bird_calls_truncated": "" if row["system_event"] else truncated,
    }
    for index, column in enumerate(CALL_COLUMNS):
        output[column] = selected_calls[index]["cell"] if index < len(selected_calls) else ""
    return output


def _row_to_julice_csv(
    row,
    zone: ZoneInfo,
    bird_calls: list[dict] | None = None,
    interval_errors: list[str] | None = None,
    options: CsvExportOptions | None = None,
) -> dict[str, str | int | float | None]:
    options = options or CsvExportOptions(profile="julice_camera_trap")
    timestamp = from_iso(row["timestamp_utc"]).astimezone(zone).strftime("%m/%d/%y %H:%M.%S")
    selected_calls, _truncated = _selected_call_cells(bird_calls or [])
    errors = list(interval_errors or [])
    if row["notes"]:
        errors.extend(str(row["notes"]).split("; "))
    output = {
        "Timestamp": timestamp,
        "Time_Source": _julice_time_source(row["timestamp_source"]),
        "Pi_Event": _julice_event(row["system_event"] or ""),
        "Temp": _round(row["temperature_c_avg"]),
        "Humidity": _round(row["humidity_pct_avg"]),
        "Lux": _round(row["lux_avg"]),
        "mmHg": _round(row["pressure_mmhg_avg"]),
        "Pi_cpu_temp": _round(row["cpu_temp_c_avg"]),
        "lat": _round(options.latitude),
        "lon": _round(options.longitude),
        "species_richness": row["bird_species_richness"] or "",
        "total_calls": row["bird_total_calls"] or "",
        "total_species": row["bird_total_species"] or "",
        "top_species": row["bird_top_species"] or "",
        "shannon_index": _round(row["bird_shannon_index"]),
        "simpsons_index": _round(row["bird_simpson_index"]),
        "pielou_evenness": _round(row["bird_pielou_evenness"]),
        "Audio_status": _julice_audio_status(row["audio_status"] or ""),
        "": "",
        "Errors": "\n".join(error for error in errors if error),
    }
    for index, column in enumerate(CALL_COLUMNS):
        output[column] = selected_calls[index]["cell"] if index < len(selected_calls) else ""
    return output


def _julice_time_source(value: str) -> str:
    mapping = {
        "gps": "GPS",
        "gps_rtc_corrected": "GPS",
        "gps_rtc_resync": "GPS",
        "rtc": "RTC",
        "estimated": "Pi CLK",
        "system": "Pi CLK",
        "backfill": "Pi CLK",
    }
    return mapping.get(value, value)


def _julice_event(value: str) -> str:
    if ";" in value or "\n" in value:
        parts = [part.strip() for part in value.replace("\n", ";").split(";") if part.strip()]
        return "\n".join(_julice_event(part) for part in parts)
    mapping = {
        "STATION_STARTED": "Pi Started",
        "STATION_SERVICE_RESTARTED": "Pi Restarted",
        "PI_RESTARTED": "Pi Restarted",
        "POSSIBLE_POWER_LOSS_RECOVERY": "Power Loss",
        "UNEXPECTED_STATION_RESTART_RECOVERY": "Power Loss",
        "GPS_COORDINATES": "GPS Cords",
        "PAST_COORDINATES": "Past Cords",
        "FALLBACK_COORDINATES": "Fallback Cords",
        "PARTIALLY_PROCESSED": "Partially Processed",
        "PROCESSING_INTERRUPTED": "Processing Interrupted",
        "PI_COOLDOWN": "Pi Cooldown",
    }
    return mapping.get(value, value)


def _julice_audio_status(value: str) -> str:
    mapping = {
        "recorded": "Recorded",
        "recording_paused": "Recording paused",
        "error": "Failed",
        "purged_at_3am": "Recording purged",
        "missing_audio": "Failed",
        "interrupted_power_loss": "Recording purged",
        "planned_reboot_partial": "Recording purged",
        "processing_interrupted": "Failed",
    }
    return mapping.get(value, value)


def _round(value: float | None) -> str:
    if value is None:
        return ""
    return f"{value:.3f}"


def _mmhg_to_inhg(value: float | None) -> float | None:
    if value is None:
        return None
    return value / MMHG_PER_INHG


def _bird_call_rows_by_interval(store: DataStore) -> dict[str, list[dict]]:
    grouped: dict[tuple[str, int], list] = {}
    for row in store.list_bird_call_candidates():
        key = (row["period_start_utc"], int(row["call_index"]))
        grouped.setdefault(key, []).append(row)

    output: dict[str, list[dict]] = {}
    for (period_start_utc, call_index), candidates in grouped.items():
        first = candidates[0]
        output.setdefault(period_start_utc, []).append(
            {
                "call_index": call_index,
                "top_confidence": first["confidence"],
                "cell": _call_cell(candidates),
            }
        )
    return output


def _errors_by_interval(store: DataStore) -> dict[str, list[str]]:
    if not hasattr(store, "list_interval_errors"):
        return {}
    grouped: dict[str, list[str]] = {}
    for row in store.list_interval_errors():
        grouped.setdefault(row["period_start_utc"], []).append(row["error"])
    return grouped


def _selected_call_cells(calls: list[dict]) -> tuple[list[dict], int]:
    if len(calls) <= MAX_BIRD_CALL_COLUMNS:
        return sorted(calls, key=lambda call: call["call_index"]), 0

    strongest = sorted(
        calls,
        key=lambda call: (
            -(call["top_confidence"] if call["top_confidence"] is not None else -1.0),
            call["call_index"],
        ),
    )[:MAX_BIRD_CALL_COLUMNS]
    return sorted(strongest, key=lambda call: call["call_index"]), len(calls) - MAX_BIRD_CALL_COLUMNS


def _call_cell(candidates) -> str:
    lines = []
    for row in candidates:
        confidence = row["confidence"]
        if confidence is None:
            lines.append(str(row["species"]))
        else:
            lines.append(f"{row['species']} ({confidence * 100:.1f}%)")
    return "\n".join(lines)
