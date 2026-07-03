from pathlib import Path

from juara_station.ai import BirdNetRunner, parse_birdnet_calls, parse_birdnet_csv
from juara_station.config import BirdNetConfig, LocationConfig


def test_parse_birdnet_calls_keeps_alternates_above_candidate_threshold(tmp_path: Path):
    csv_path = tmp_path / "BirdNET.results.csv"
    csv_path.write_text(
        "\n".join(
            [
                "Start (s),End (s),Common name,Confidence",
                "0,3,Hyacinth macaw,0.82",
                "0,3,Blue-and-yellow macaw,0.14",
                "0,3,Low candidate,0.09",
                "3,6,Too weak top bird,0.24",
                "6,9,Rufous hornero,0.31",
            ]
        )
        + "\n"
    )

    calls = parse_birdnet_calls(csv_path, min_confidence=0.25, candidate_min_confidence=0.10)

    assert len(calls) == 2
    assert [candidate.species for candidate in calls[0].candidates] == [
        "Hyacinth macaw",
        "Blue-and-yellow macaw",
    ]
    assert calls[1].top_candidate.species == "Rufous hornero"


def test_parse_birdnet_csv_counts_only_top_candidates(tmp_path: Path):
    csv_path = tmp_path / "BirdNET.results.csv"
    csv_path.write_text(
        "\n".join(
            [
                "Start (s),End (s),Common name,Confidence",
                "0,3,Hyacinth macaw,0.82",
                "0,3,Blue-and-yellow macaw,0.14",
                "3,6,Hyacinth macaw,0.64",
            ]
        )
        + "\n"
    )

    detections = parse_birdnet_csv(csv_path, min_confidence=0.25, candidate_min_confidence=0.10)

    assert len(detections) == 1
    assert detections[0].species == "Hyacinth macaw"
    assert detections[0].calls == 2


def test_birdnet_can_switch_between_generated_list_and_native_location_filter():
    generated = BirdNetRunner(
        BirdNetConfig(species_filter_mode="generated_list", species_list_path="/tmp/species.txt"),
        LocationConfig(latitude=-16.68, longitude=-56.90),
    )
    native = BirdNetRunner(
        BirdNetConfig(species_filter_mode="birdnet_location", species_list_path="/tmp/species.txt"),
        LocationConfig(latitude=-16.68, longitude=-56.90),
    )

    assert generated._species_list_path() == "/tmp/species.txt"
    assert native._species_list_path() is None
