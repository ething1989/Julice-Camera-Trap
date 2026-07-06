from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import csv
import math
import subprocess
import sys
import tempfile

from .paths import atomic_replace_text


@dataclass(frozen=True)
class SpeciesPackSelection:
    latitude: float
    longitude: float
    cell_files: tuple[str, ...]
    species_count: int
    source: str = "species_pack"


def build_species_list_from_pack(
    pack_root: Path,
    latitude: float,
    longitude: float,
    *,
    nearest_cell_count: int = 4,
) -> tuple[list[str], SpeciesPackSelection]:
    pack_root = Path(pack_root)
    cells = _load_cells(pack_root)
    if not cells:
        raise FileNotFoundError(f"No cell index rows found in {pack_root / 'metadata' / 'cell_index.csv'}")
    nearest_cells = sorted(
        cells,
        key=lambda row: _haversine_km(latitude, longitude, float(row["center_lat"]), float(row["center_lon"])),
    )[: max(1, nearest_cell_count)]

    species: set[str] = set()
    cell_files = tuple(row["file"] for row in nearest_cells)
    for relative in cell_files:
        species.update(_read_species_file(pack_root / relative))

    selected = sorted(value for value in species if value)
    return selected, SpeciesPackSelection(
        latitude=latitude,
        longitude=longitude,
        cell_files=cell_files,
        species_count=len(selected),
    )


def write_active_species_list(
    pack_root: Path,
    output_path: Path,
    latitude: float,
    longitude: float,
    *,
    nearest_cell_count: int = 4,
) -> SpeciesPackSelection:
    species, selection = build_species_list_from_pack(
        pack_root,
        latitude,
        longitude,
        nearest_cell_count=nearest_cell_count,
    )
    atomic_replace_text(Path(output_path), "\n".join(species) + "\n")
    metadata = {
        "latitude": selection.latitude,
        "longitude": selection.longitude,
        "nearest_cell_count": nearest_cell_count,
        "cell_files": list(selection.cell_files),
        "species_count": selection.species_count,
        "source": selection.source,
    }
    atomic_replace_text(Path(output_path).with_suffix(Path(output_path).suffix + ".metadata.json"), _json(metadata))
    return selection


def write_area_species_list_from_pack(
    pack_root: Path,
    output_path: Path,
    latitude: float,
    longitude: float,
    *,
    radius_km: float = 100.0,
) -> SpeciesPackSelection:
    """Build a fast local area list from the bundled offline cell pack."""
    pack_root = Path(pack_root)
    sample_points = _area_sample_points(latitude, longitude, radius_km)
    cells = _load_cells(pack_root)
    if not cells:
        raise FileNotFoundError(f"No cell index rows found in {pack_root / 'metadata' / 'cell_index.csv'}")

    selected_cells: dict[str, dict[str, str]] = {}
    for point_lat, point_lon, _label in sample_points:
        nearest = min(
            cells,
            key=lambda row: _haversine_km(point_lat, point_lon, float(row["center_lat"]), float(row["center_lon"])),
        )
        selected_cells[nearest["file"]] = nearest

    species: set[str] = set()
    for relative in sorted(selected_cells):
        species.update(_read_species_file(pack_root / relative))

    selected = sorted(value for value in species if value)
    if not selected:
        raise RuntimeError("Offline area species-pack filter produced no species")
    atomic_replace_text(Path(output_path), "\n".join(selected) + "\n")
    selection = SpeciesPackSelection(
        latitude=latitude,
        longitude=longitude,
        cell_files=tuple(sorted(selected_cells)),
        species_count=len(selected),
        source="species_pack_area",
    )
    metadata = {
        "latitude": selection.latitude,
        "longitude": selection.longitude,
        "cell_files": list(selection.cell_files),
        "species_count": selection.species_count,
        "source": selection.source,
        "filter": "offline_species_pack_area",
        "radius_km": radius_km,
        "sample_point_count": len(sample_points),
        "note": "Fast offline 100 km area filter from bundled BirdNET species-pack cells; not a manual species blacklist.",
    }
    atomic_replace_text(Path(output_path).with_suffix(Path(output_path).suffix + ".metadata.json"), _json(metadata))
    return selection


def write_world_species_list(pack_root: Path, output_path: Path) -> SpeciesPackSelection:
    pack_root = Path(pack_root)
    species = _read_species_file(pack_root / "regions" / "world.txt")
    if not species:
        values: set[str] = set()
        for row in _load_cells(pack_root):
            values.update(_read_species_file(pack_root / row["file"]))
        species = sorted(values)
    selected = sorted(value for value in species if value)
    atomic_replace_text(Path(output_path), "\n".join(selected) + "\n")
    selection = SpeciesPackSelection(
        latitude=-1,
        longitude=-1,
        cell_files=(),
        species_count=len(selected),
        source="world",
    )
    metadata = {
        "latitude": selection.latitude,
        "longitude": selection.longitude,
        "cell_files": list(selection.cell_files),
        "species_count": selection.species_count,
        "source": selection.source,
        "filter": "all_birds",
    }
    atomic_replace_text(Path(output_path).with_suffix(Path(output_path).suffix + ".metadata.json"), _json(metadata))
    return selection


def write_birdnet_area_species_list(
    output_path: Path,
    latitude: float,
    longitude: float,
    *,
    radius_km: float = 100.0,
    week: int = -1,
    threshold: float = 0.03,
    timeout_seconds: int = 180,
) -> SpeciesPackSelection:
    output_path = Path(output_path)
    sample_points = _area_sample_points(latitude, longitude, radius_km)
    selected: set[str] = set()
    failures: list[str] = []
    with tempfile.TemporaryDirectory(prefix="juara-birdnet-area-species-") as tmp:
        tmp_path = Path(tmp)
        for index, (point_lat, point_lon, label) in enumerate(sample_points):
            point_dir = tmp_path / f"point_{index:02d}_{label}"
            point_dir.mkdir()
            try:
                _write_birdnet_species_files(
                    point_dir,
                    point_lat,
                    point_lon,
                    week=week,
                    threshold=threshold,
                    timeout_seconds=timeout_seconds,
                )
            except Exception as exc:  # pragma: no cover - exercised on Pis with model installed
                failures.append(f"{label}:{exc}")
                continue
            for path in sorted(point_dir.glob("*.txt")):
                selected.update(_read_species_file(path))
    species = sorted(value for value in selected if value)
    if not species:
        detail = "; ".join(failures) if failures else "no species files written"
        raise RuntimeError(f"BirdNET area species filter produced no species: {detail}")
    atomic_replace_text(output_path, "\n".join(species) + "\n")
    selection = SpeciesPackSelection(
        latitude=latitude,
        longitude=longitude,
        cell_files=(),
        species_count=len(species),
        source="birdnet_area",
    )
    metadata = {
        "latitude": selection.latitude,
        "longitude": selection.longitude,
        "cell_files": list(selection.cell_files),
        "species_count": selection.species_count,
        "source": selection.source,
        "filter": "birdnet_area_frequency",
        "radius_km": radius_km,
        "sample_point_count": len(sample_points),
        "week": week,
        "threshold": threshold,
        "failed_sample_points": failures,
        "note": "BirdNET offline location-frequency filter sampled at center plus 100 km offsets; not a manual species blacklist.",
    }
    atomic_replace_text(Path(output_path).with_suffix(Path(output_path).suffix + ".metadata.json"), _json(metadata))
    return selection


def _load_cells(pack_root: Path) -> list[dict[str, str]]:
    path = pack_root / "metadata" / "cell_index.csv"
    with path.open(newline="") as handle:
        return list(csv.DictReader(handle))


def _read_species_file(path: Path) -> list[str]:
    if not path.exists():
        return []
    species = []
    for line in path.read_text(errors="replace").splitlines():
        value = line.strip()
        if not value or value.startswith("#") or "\t" in value:
            continue
        species.append(value)
    return species


def _write_birdnet_species_files(
    output_dir: Path,
    latitude: float,
    longitude: float,
    *,
    week: int,
    threshold: float,
    timeout_seconds: int,
) -> None:
    code = (
        "from birdnet_analyzer.species import species; "
        "import sys; "
        "species(sys.argv[1], lat=float(sys.argv[2]), lon=float(sys.argv[3]), "
        "week=int(sys.argv[4]), sf_thresh=float(sys.argv[5]), sortby='alpha')"
    )
    subprocess.run(
        [
            sys.executable,
            "-c",
            code,
            str(output_dir),
            str(latitude),
            str(longitude),
            str(week),
            str(threshold),
        ],
        check=True,
        timeout=timeout_seconds,
    )


def _area_sample_points(latitude: float, longitude: float, radius_km: float) -> tuple[tuple[float, float, str], ...]:
    radius = max(0.0, float(radius_km))
    offsets = [
        (0.0, 0.0, "center"),
        (radius, 0.0, "north"),
        (-radius, 0.0, "south"),
        (0.0, radius, "east"),
        (0.0, -radius, "west"),
        (radius, radius, "northeast"),
        (radius, -radius, "northwest"),
        (-radius, radius, "southeast"),
        (-radius, -radius, "southwest"),
    ]
    if radius == 0:
        offsets = offsets[:1]
    return tuple(
        (*_offset_lat_lon(latitude, longitude, north_km, east_km), label)
        for north_km, east_km, label in offsets
    )


def _offset_lat_lon(latitude: float, longitude: float, north_km: float, east_km: float) -> tuple[float, float]:
    lat = latitude + north_km / 110.574
    cos_lat = max(0.01, abs(math.cos(math.radians(latitude))))
    lon = longitude + east_km / (111.320 * cos_lat)
    lon = ((lon + 180.0) % 360.0) - 180.0
    return lat, lon


def _haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    radius_km = 6371.0088
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    return 2 * radius_km * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def _json(value: dict) -> str:
    import json

    return json.dumps(value, indent=2, sort_keys=True) + "\n"
