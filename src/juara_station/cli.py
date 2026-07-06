from __future__ import annotations

from datetime import datetime
from pathlib import Path
import argparse
import logging

from .config import load_config
from .csv_exporter import export_day_csv
from .geolocation import DEFAULT_GEOLOCATION_URLS, read_internet_location
from .paths import resolve_paths
from .service import StationService
from .species_pack import write_active_species_list, write_birdnet_area_species_list
from .storage import DataStore, to_utc_iso, utc_now


def main() -> None:
    parser = argparse.ArgumentParser(prog="juara-station")
    parser.add_argument("--config", type=Path, default=None, help="Path to station TOML config.")
    parser.add_argument("--mock", action="store_true", help="Use mock hardware and mock AI runners.")
    parser.add_argument("--log-level", default="INFO")
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="Run continuously.")
    run.set_defaults(func=_run)

    once = sub.add_parser("once", help="Run one interval, useful for local testing.")
    once.add_argument("--duration", type=int, default=None, help="Override interval duration in seconds.")
    once.set_defaults(func=_once)

    process = sub.add_parser("process-backlog", help="Retry pending audio AI work.")
    process.set_defaults(func=_process_backlog)

    ai_worker = sub.add_parser("ai-worker", help="Run the BirdNET backlog worker.")
    ai_worker.add_argument("--sleep-seconds", type=int, default=60, help="Seconds between idle AI backlog checks.")
    ai_worker.add_argument("--once", action="store_true", help="Run one AI backlog cycle and exit.")
    ai_worker.set_defaults(func=_ai_worker)

    planned_reboot = sub.add_parser("planned-reboot-cleanup", help="Clean partial work before a scheduled reboot.")
    planned_reboot.set_defaults(func=_planned_reboot_cleanup)

    export = sub.add_parser("export-csv", help="Export the current local day CSV from SQLite.")
    export.add_argument("--date", default=None, help="Local date YYYY-MM-DD; defaults to today.")
    export.set_defaults(func=_export)

    doctor = sub.add_parser("doctor", help="Print resolved paths and basic runtime state.")
    doctor.set_defaults(func=_doctor)

    species = sub.add_parser("select-species", help="Build the active BirdNET species list from coordinates.")
    species.add_argument("--lat", type=float, default=None, help="Latitude; defaults to configured fallback latitude.")
    species.add_argument("--lon", type=float, default=None, help="Longitude; defaults to configured fallback longitude.")
    species.add_argument(
        "--internet-location",
        action="store_true",
        help="Use IP geolocation when latitude/longitude are not provided.",
    )
    species.add_argument(
        "--write-coordinate-state",
        action="store_true",
        help="Persist the selected coordinates for the station service.",
    )
    species.add_argument(
        "--radius-km",
        type=float,
        default=None,
        help="Radius for BirdNET area species generation; defaults to config time.species_area_radius_km.",
    )
    species.set_defaults(func=_select_species)

    args = parser.parse_args()
    logging.basicConfig(level=getattr(logging, args.log_level.upper(), logging.INFO), format="%(asctime)s %(levelname)s %(message)s")
    args.func(args)


def _service(args, ai_only: bool = False) -> StationService:
    config = load_config(args.config)
    paths = resolve_paths(config.storage)
    return StationService(config, paths, mock=args.mock, ai_only=ai_only)


def _run(args) -> None:
    _service(args).run_forever()


def _once(args) -> None:
    service = _service(args)
    path = service.run_interval(duration_seconds=args.duration)
    print(path)


def _process_backlog(args) -> None:
    service = _service(args)
    days = set()
    days.update(service.process_audio_backlog())
    for day in sorted(days):
        export_day_csv(
            service.store,
            service.paths.logs_dir,
            datetime.combine(day, datetime.min.time(), tzinfo=service.config.zoneinfo),
            service.config.zoneinfo,
            options=service._csv_export_options(),
        )
        service._trigger_drive_sync("manual backlog CSV export")
    print(f"processed_days={len(days)}")


def _ai_worker(args) -> None:
    service = _service(args, ai_only=True)
    if args.once:
        days = service.run_ai_worker_once()
        print(f"processed_days={len(days)}")
        return
    service.run_ai_worker_forever(sleep_seconds=args.sleep_seconds)


def _planned_reboot_cleanup(args) -> None:
    service = _service(args, ai_only=True)
    days = service.planned_reboot_cleanup()
    print(f"planned_reboot_cleanup_days={len(days)}")


def _export(args) -> None:
    config = load_config(args.config)
    paths = resolve_paths(config.storage)
    store = DataStore(paths.database_path)
    if args.date:
        local_day = datetime.fromisoformat(args.date).replace(tzinfo=config.zoneinfo)
    else:
        local_day = utc_now().astimezone(config.zoneinfo)
    service = StationService(config, paths, mock=args.mock, ai_only=True)
    print(
        export_day_csv(
            store,
            paths.logs_dir,
            local_day,
            config.zoneinfo,
            options=service._csv_export_options(),
        )
    )


def _doctor(args) -> None:
    config = load_config(args.config)
    paths = resolve_paths(config.storage)
    store = DataStore(paths.database_path)
    state = store.get_time_state()
    print(f"root={paths.root}")
    print(f"fallback_active={paths.fallback_active}")
    print(f"database={paths.database_path}")
    print(f"last_timestamp_utc={state['last_timestamp_utc']}")
    print(f"bad_gps_count={state['bad_gps_count']}")


def _select_species(args) -> None:
    config = load_config(args.config)
    pack_root = config.time.species_pack_root
    if pack_root is None:
        raise SystemExit("time.species_pack_root is not configured")
    output_path = config.time.active_species_list_path
    if output_path is None:
        if config.birdnet.species_list_path is None:
            raise SystemExit("No active species list output path is configured")
        output_path = Path(config.birdnet.species_list_path)
    latitude = args.lat
    longitude = args.lon
    coordinate_source = "cli"
    if (latitude is None or longitude is None) and args.internet_location:
        urls = config.time.internet_coordinate_urls or list(DEFAULT_GEOLOCATION_URLS)
        location = read_internet_location(urls, timeout_seconds=config.time.internet_coordinate_timeout_seconds)
        if location is not None:
            latitude = location.latitude
            longitude = location.longitude
            coordinate_source = "internet"
            label = f" ({location.label})" if location.label else ""
            print(f"internet_location={location.source_url}{label}")
    if latitude is None:
        latitude = config.time.fallback_latitude
        coordinate_source = "fallback"
    if longitude is None:
        longitude = config.time.fallback_longitude
        coordinate_source = "fallback"
    if args.write_coordinate_state or coordinate_source == "internet":
        paths = resolve_paths(config.storage)
        state_path = paths.state_dir / "active_coordinates.json"
        state_path.parent.mkdir(parents=True, exist_ok=True)
        state_path.write_text(
            "{\n"
            f'  "latitude": {float(latitude)},\n'
            f'  "longitude": {float(longitude)},\n'
            f'  "source": "{coordinate_source}",\n'
            f'  "updated_at_utc": "{to_utc_iso(utc_now())}"\n'
            "}\n"
        )
    radius_km = args.radius_km if args.radius_km is not None else config.time.species_area_radius_km
    try:
        selection = write_birdnet_area_species_list(output_path, latitude, longitude, radius_km=radius_km, week=-1)
    except Exception:
        logging.exception("BirdNET area species generation failed; falling back to local species pack")
        selection = write_active_species_list(pack_root, output_path, latitude, longitude)
    print(f"coordinate_source={coordinate_source}")
    print(f"species_count={selection.species_count}")
    print(f"source={selection.source}")
    print("nearest_cell_count=" + str(len(selection.cell_files)))
    print("cells=" + ",".join(selection.cell_files))
    print(f"output={output_path}")


if __name__ == "__main__":
    main()
