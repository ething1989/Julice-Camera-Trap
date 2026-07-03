#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import shutil
import time


MILES_PER_DEG_LAT = 69.0
MILES_PER_DEG_LON_EQUATOR = 69.172


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build a no-region BirdNET species pack from approximate square mile cells."
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("/opt/juara-wildlife-station/data/BirdNET_100mi_PrimaryPlus"),
        help="Output species-pack directory.",
    )
    parser.add_argument("--cell-miles", type=float, default=100.0, help="Approximate cell width and height in miles.")
    parser.add_argument("--week", type=int, default=-1, help="BirdNET week. Use -1 for year-round.")
    parser.add_argument(
        "--min-location-score",
        type=float,
        default=0.03,
        help="Package-generation location score cutoff. This does not change BirdNET analysis confidence.",
    )
    parser.add_argument(
        "--max-species-per-cell",
        type=int,
        default=500,
        help="Keep only the highest-ranked species in each cell. Use 0 for no cap.",
    )
    parser.add_argument("--batch-size", type=int, default=256, help="Metadata model inference batch size.")
    parser.add_argument("--threads", type=int, default=1, help="TFLite threads for metadata model inference.")
    parser.add_argument("--force", action="store_true", help="Replace the output directory if it already exists.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.cell_miles <= 0:
        raise SystemExit("--cell-miles must be positive")
    if args.batch_size <= 0:
        raise SystemExit("--batch-size must be positive")

    output = args.output
    if output.exists() and not args.force:
        raise SystemExit(f"{output} already exists; pass --force to replace it")

    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    temp_output = output.parent / f".{output.name}.tmp_{stamp}"
    backup = output.parent / f"{output.name}.backup_{stamp}"
    if temp_output.exists():
        shutil.rmtree(temp_output)

    cells_dir = temp_output / "cells"
    metadata_dir = temp_output / "metadata"
    cells_dir.mkdir(parents=True)
    metadata_dir.mkdir(parents=True)

    cells = list(iter_cells(args.cell_miles))
    started = time.monotonic()

    import numpy as np
    from birdnet_analyzer import model, utils
    import birdnet_analyzer.config as cfg

    cfg.TFLITE_THREADS = max(1, args.threads)
    cfg.LABELS = utils.read_lines(cfg.BIRDNET_LABELS_FILE)
    model.load_meta_model()
    interpreter = model.M_INTERPRETER
    input_index = model.M_INPUT_LAYER_INDEX
    output_index = model.M_OUTPUT_LAYER_INDEX

    rows: list[dict[str, object]] = []
    unique_species: set[str] = set()
    raw_counts: list[int] = []
    final_counts: list[int] = []
    cap_applied_count = 0

    current_shape: int | None = None
    for batch_start in range(0, len(cells), args.batch_size):
        batch = cells[batch_start : batch_start + args.batch_size]
        samples = np.array([[cell["center_lat"], cell["center_lon"], args.week] for cell in batch], dtype="float32")
        if current_shape != len(samples):
            interpreter.resize_tensor_input(input_index, [len(samples), 3], strict=False)
            interpreter.allocate_tensors()
            current_shape = len(samples)
        interpreter.set_tensor(input_index, samples)
        interpreter.invoke()
        predictions = interpreter.get_tensor(output_index)

        for cell, prediction in zip(batch, predictions, strict=True):
            ranked = sorted(
                (
                    (float(score), label)
                    for score, label in zip(prediction, cfg.LABELS, strict=True)
                    if float(score) >= args.min_location_score
                ),
                key=lambda item: item[0],
                reverse=True,
            )
            raw_count = len(ranked)
            selected = ranked[: args.max_species_per_cell] if args.max_species_per_cell > 0 else ranked
            cap_applied = len(selected) < raw_count
            if cap_applied:
                cap_applied_count += 1

            species = [label for _score, label in selected]
            unique_species.update(species)
            raw_counts.append(raw_count)
            final_counts.append(len(species))

            species_file = cells_dir / f"{cell['cell_id']}.txt"
            species_file.write_text("\n".join(species) + ("\n" if species else ""))

            rows.append(
                {
                    **cell,
                    "raw_species_count_before_cap": raw_count,
                    "species_count": len(species),
                    "cap_applied": cap_applied,
                    "lowest_kept_location_score": round(selected[-1][0], 6) if selected else "",
                    "file": f"cells/{species_file.name}",
                }
            )

        done = min(batch_start + len(batch), len(cells))
        if done == len(cells) or done % (args.batch_size * 10) == 0:
            elapsed = time.monotonic() - started
            print(f"built {done}/{len(cells)} cells in {elapsed:.1f}s", flush=True)

    write_cell_indexes(metadata_dir, rows)
    duration = time.monotonic() - started
    summary = {
        "generated_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "cell_miles": args.cell_miles,
        "meaning_of_cells": "Approximate 100 mile x 100 mile cells; longitude width is adjusted per latitude band.",
        "week": args.week,
        "meaning_of_week": "year-round / any time of year" if args.week == -1 else "BirdNET four-week period",
        "min_birdnet_location_score": args.min_location_score,
        "max_species_per_cell": args.max_species_per_cell,
        "cell_count": len(rows),
        "world_unique_species_in_pack": len(unique_species),
        "raw_species_count_min": min(raw_counts) if raw_counts else 0,
        "raw_species_count_avg": round(sum(raw_counts) / len(raw_counts), 2) if raw_counts else 0,
        "raw_species_count_max": max(raw_counts) if raw_counts else 0,
        "final_species_count_min": min(final_counts) if final_counts else 0,
        "final_species_count_avg": round(sum(final_counts) / len(final_counts), 2) if final_counts else 0,
        "final_species_count_max": max(final_counts) if final_counts else 0,
        "cells_where_cap_applied": cap_applied_count,
        "region_files": 0,
        "duration_seconds": round(duration, 2),
    }
    (metadata_dir / "build_summary.json").write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n")
    (temp_output / "README.txt").write_text(
        "BirdNET no-region species pack for Juara stations.\n"
        "The station selects the nearest 4 cells to the active GPS/fallback coordinates and unions their species lists.\n"
        "This pack intentionally contains no region_index file.\n"
    )

    if output.exists():
        output.rename(backup)
    temp_output.rename(output)
    if backup.exists():
        print(f"replaced {output}; previous pack backed up at {backup}", flush=True)
    else:
        print(f"wrote {output}", flush=True)
    print(json.dumps(summary, indent=2, sort_keys=True), flush=True)
    return 0


def iter_cells(cell_miles: float) -> list[dict[str, object]]:
    lat_cell_count = max(1, math.ceil(180.0 / (cell_miles / MILES_PER_DEG_LAT)))
    lat_step = 180.0 / lat_cell_count
    cells: list[dict[str, object]] = []
    for lat_index in range(lat_cell_count):
        lat_min = -90.0 + lat_index * lat_step
        lat_max = 90.0 if lat_index == lat_cell_count - 1 else lat_min + lat_step
        center_lat = (lat_min + lat_max) / 2.0
        lon_miles_per_degree = max(0.01, MILES_PER_DEG_LON_EQUATOR * abs(math.cos(math.radians(center_lat))))
        desired_lon_step = min(360.0, cell_miles / lon_miles_per_degree)
        lon_cell_count = max(1, math.ceil(360.0 / desired_lon_step))
        lon_step = 360.0 / lon_cell_count
        for lon_index in range(lon_cell_count):
            lon_min = -180.0 + lon_index * lon_step
            lon_max = 180.0 if lon_index == lon_cell_count - 1 else lon_min + lon_step
            center_lon = (lon_min + lon_max) / 2.0
            cells.append(
                {
                    "cell_id": f"cell_{coord_token(center_lat, 'N', 'S')}_{coord_token(center_lon, 'E', 'W')}_m{int(round(cell_miles))}",
                    "center_lat": round(center_lat, 6),
                    "center_lon": round(center_lon, 6),
                    "lat_min": round(lat_min, 6),
                    "lat_max": round(lat_max, 6),
                    "lon_min": round(lon_min, 6),
                    "lon_max": round(lon_max, 6),
                    "height_miles_approx": round((lat_max - lat_min) * MILES_PER_DEG_LAT, 2),
                    "width_miles_approx": round((lon_max - lon_min) * lon_miles_per_degree, 2),
                }
            )
    return cells


def coord_token(value: float, positive: str, negative: str) -> str:
    prefix = positive if value >= 0 else negative
    return prefix + f"{abs(value):06.3f}".replace(".", "p")


def write_cell_indexes(metadata_dir: Path, rows: list[dict[str, object]]) -> None:
    fieldnames = [
        "cell_id",
        "center_lat",
        "center_lon",
        "lat_min",
        "lat_max",
        "lon_min",
        "lon_max",
        "height_miles_approx",
        "width_miles_approx",
        "raw_species_count_before_cap",
        "species_count",
        "cap_applied",
        "lowest_kept_location_score",
        "file",
    ]
    with (metadata_dir / "cell_index.csv").open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
    (metadata_dir / "cell_index.json").write_text(json.dumps(rows, indent=2, sort_keys=True) + "\n")


if __name__ == "__main__":
    raise SystemExit(main())
