import json
from pathlib import Path

from juara_station.species_pack import build_species_list_from_pack, write_active_species_list


def test_species_pack_selects_four_nearest_cells_without_regions(tmp_path: Path):
    pack = tmp_path / "pack"
    (pack / "metadata").mkdir(parents=True)
    (pack / "cells").mkdir()
    (pack / "metadata" / "cell_index.csv").write_text(
        "\n".join(
            [
                "cell_id,center_lat,center_lon,lat_min,lat_max,lon_min,lon_max,radius_km_approx,species_count,file",
                "a,-17.5,-57.5,-20,-15,-60,-55,278,2,cells/a.txt",
                "b,-17.5,-52.5,-20,-15,-55,-50,278,2,cells/b.txt",
                "c,-12.5,-57.5,-15,-10,-60,-55,278,2,cells/c.txt",
                "d,-22.5,-57.5,-25,-20,-60,-55,278,2,cells/d.txt",
                "e,42.5,-82.5,40,45,-85,-80,278,2,cells/e.txt",
            ]
        )
        + "\n"
    )
    for name, species in {
        "a": "Cell A bird\nSpecies one\n",
        "b": "Cell B bird\n",
        "c": "Cell C bird\n",
        "d": "Cell D bird\n",
        "e": "Cell E bird\n",
    }.items():
        (pack / "cells" / f"{name}.txt").write_text(species)

    species, selection = build_species_list_from_pack(pack, -17.102778, -56.941639)

    assert len(selection.cell_files) == 4
    assert selection.cell_files == ("cells/a.txt", "cells/b.txt", "cells/c.txt", "cells/d.txt")
    assert "Species one" in species
    assert "Cell E bird" not in species


def test_species_pack_writes_active_list_and_metadata(tmp_path: Path):
    pack = tmp_path / "pack"
    (pack / "metadata").mkdir(parents=True)
    (pack / "cells").mkdir()
    (pack / "metadata" / "cell_index.csv").write_text(
        "cell_id,center_lat,center_lon,lat_min,lat_max,lon_min,lon_max,radius_km_approx,species_count,file\n"
        "a,0,0,-2.5,2.5,-2.5,2.5,278,1,cells/a.txt\n"
    )
    (pack / "cells" / "a.txt").write_text("Only bird\n")
    output = tmp_path / "active.txt"

    selection = write_active_species_list(pack, output, 0.1, 0.1)

    assert output.read_text() == "Only bird\n"
    assert selection.species_count == 1
    metadata = json.loads(output.with_suffix(".txt.metadata.json").read_text())
    assert metadata["nearest_cell_count"] == 4
    assert "region_key" not in metadata
