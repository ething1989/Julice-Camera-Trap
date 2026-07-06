from datetime import datetime, timedelta, timezone
from pathlib import Path
import csv

from juara_station.csv_exporter import CsvExportOptions, export_main_csv
from juara_station.storage import BirdCall, BirdCandidate, DataStore, SensorSample, SoundDetection


def test_julice_csv_has_sensor_bird_calls_and_no_photo_columns(tmp_path: Path):
    store = DataStore(tmp_path / "station.sqlite3")
    start = datetime(2026, 7, 3, 12, 0, tzinfo=timezone.utc)
    end = start + timedelta(minutes=5)
    store.insert_sensor_sample(
        SensorSample(
            sampled_at=start,
            temperature_c=25.2,
            humidity_pct=71.0,
            pressure_mmhg=755.4,
            lux=1200.0,
            cpu_temp_c=43.1,
        )
    )
    store.upsert_audio_event(start, "recorded", "/tmp/audio.wav", start, end, ai_status="done")
    store.save_bird_calls(
        start,
        [
            BirdCall(
                0.0,
                3.0,
                (
                    BirdCandidate("Hyacinth macaw", 0.82),
                    BirdCandidate("Blue-and-yellow macaw", 0.14),
                ),
            ),
            BirdCall(3.0, 6.0, (BirdCandidate("Hyacinth macaw", 0.64),)),
        ],
    )
    store.save_sound_detections(
        start,
        "yamnet",
        [
            SoundDetection("Bird vocalization, bird call, bird song", 0.91, category="bird"),
            SoundDetection("Insect", 0.24, category="insect"),
        ],
    )
    store.upsert_interval_summary(start, end, start, "gps")

    csv_path = export_main_csv(
        store,
        tmp_path,
        timezone.utc,
        CsvExportOptions(
            filename="Julice Camera Trap July 2026.csv",
            profile="julice_camera_trap",
            latitude=-16.68260,
            longitude=-56.90453,
        ),
    )

    rows = list(csv.DictReader(csv_path.open()))
    assert len(rows) == 1
    header = rows[0].keys()
    assert "photos_taken" not in header
    assert "animal_detections" not in header
    assert "co2_ppm_avg" not in header
    assert rows[0]["Timestamp"] == "07/03/26 12:00.00"
    assert rows[0]["mmHg"] == "755.400"
    assert rows[0]["lat"] == "-16.683"
    assert rows[0]["lon"] == "-56.905"
    assert rows[0]["top_species"] == "Hyacinth macaw(Calls: 2, Conf: 73.0%)"
    assert rows[0]["top_family"] == "Psittacidae(Calls: 2, Support: 80.0%)"
    assert rows[0]["yamnet_top_label"] == "Bird vocalization, bird call, bird song"
    assert rows[0]["yamnet_bird_score"] == "0.910"
    assert rows[0]["yamnet_insect_score"] == "0.240"
    assert rows[0]["Call 1"] == "Hyacinth macaw (82.0%)\nBlue-and-yellow macaw (14.0%)"
    assert rows[0]["Call 90"] == ""


def test_event_rows_coalesce_into_interval_without_sensor_data(tmp_path: Path):
    store = DataStore(tmp_path / "station.sqlite3")
    first = datetime(2026, 7, 3, 12, 1, tzinfo=timezone.utc)
    second = datetime(2026, 7, 3, 12, 2, tzinfo=timezone.utc)
    store.insert_system_event(first, "PI_RESTARTED")
    store.insert_system_event(second, "POSSIBLE_POWER_LOSS_RECOVERY")

    csv_path = export_main_csv(
        store,
        tmp_path,
        timezone.utc,
        CsvExportOptions(filename="Julice Camera Trap July 2026.csv", profile="julice_camera_trap"),
    )

    rows = list(csv.DictReader(csv_path.open()))
    assert len(rows) == 1
    assert rows[0]["Pi_Event"] == "Pi Restarted\nPower Loss"
