#!/usr/bin/env python3
from __future__ import annotations

import birdnet_analyzer.config as cfg
from birdnet_analyzer.species.utils import get_species_list
from birdnet_analyzer.utils import read_lines


EXPECTED = [
    "Amazonian Grosbeak",
    "Amazonian Motmot",
    "Amazonian Pygmy-Owl",
    "Black-billed Thrush",
    "Black-faced Antthrush",
    "Blue-gray Saltator",
    "Buff-breasted Wren",
    "Buff-throated Woodcreeper",
    "Cinereous Tinamou",
    "Ferruginous Pygmy-Owl",
    "Gray Antbird",
    "Hauxwell's Thrush",
    "Little Tinamou",
    "Red-bellied Macaw",
    "Screaming Piha",
    "Solitary Black Cacique",
    "Striped Woodcreeper",
    "Tawny-bellied Screech-Owl",
    "Thrush-like Wren",
    "Undulated Tinamou",
    "White-winged Becard",
]


def main() -> int:
    cfg.LABELS = read_lines(cfg.BIRDNET_LABELS_FILE)
    species = get_species_list(-16.68260, -56.90453, 18, 0.005)
    common_names = {item.split("_", 1)[1] if "_" in item else item for item in species}
    print(f"species list count: {len(species)}")
    for name in EXPECTED:
        print(f"{'YES' if name in common_names else 'NO '} {name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
