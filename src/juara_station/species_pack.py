from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import csv
import math

from .paths import atomic_replace_text


@dataclass(frozen=True)
class SpeciesPackSelection:
    latitude: float
    longitude: float
    cell_files: tuple[str, ...]
    species_count: int


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
