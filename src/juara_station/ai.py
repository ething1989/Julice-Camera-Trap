from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
import csv
import logging
import os
import shutil
import subprocess
import sys
import tempfile
from threading import Lock
import time
import wave

from .config import BirdNetConfig, LocationConfig
from .storage import BirdCall, BirdCandidate, BirdDetection, calls_to_detections


LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class BirdNetAudioJob:
    period_start: datetime
    audio_path: Path


class BirdNetRunner:
    def __init__(self, config: BirdNetConfig, location: LocationConfig):
        self.config = config
        self.location = location
        self._latitude = location.latitude
        self._longitude = location.longitude
        self._inprocess_ready = False
        self._analyze_lock = Lock()

    def set_location(self, latitude: float, longitude: float) -> None:
        self._latitude = float(latitude)
        self._longitude = float(longitude)

    def analyze_audio(self, audio_path: Path, output_dir: Path, recorded_at: datetime, night: bool) -> list[BirdCall]:
        output_dir.mkdir(parents=True, exist_ok=True)
        week = birdnet_week(recorded_at)
        try:
            with tempfile.TemporaryDirectory(prefix="juara-birdnet-single-") as temp_dir:
                input_path = Path(temp_dir) / f"{audio_path.stem}.wav"
                self._prepare_audio_input(audio_path, input_path)
                self._analyze_with_birdnet(input_path, output_dir, week, night, timeout=1800)
            csv_path = _latest_csv(output_dir)
            if csv_path is None:
                return []
            return parse_birdnet_calls(
                csv_path,
                min_confidence=self.config.min_confidence,
                candidate_min_confidence=self.config.candidate_min_confidence,
            )
        finally:
            if not self.config.keep_work_outputs:
                shutil.rmtree(output_dir, ignore_errors=True)

    def analyze_audio_batch(
        self, jobs: list[BirdNetAudioJob], output_dir: Path, week: int, night: bool
    ) -> dict[datetime, list[BirdCall]]:
        if not jobs:
            return {}
        output_dir.mkdir(parents=True, exist_ok=True)
        try:
            with tempfile.TemporaryDirectory(prefix="juara-birdnet-") as temp_dir:
                input_dir = Path(temp_dir) / "input"
                input_dir.mkdir()
                stems: dict[datetime, str] = {}
                for job in jobs:
                    stem = job.period_start.strftime("%Y%m%d_%H%M%S")
                    stems[job.period_start] = stem
                    input_path = input_dir / f"{stem}.wav"
                    self._prepare_audio_input(job.audio_path, input_path)

                timeout = max(1800, 900 * len(jobs))
                self._analyze_with_birdnet(input_dir, output_dir, week, night, timeout=timeout)

            detections = {}
            for period_start, stem in stems.items():
                csv_path = _csv_for_stem(output_dir, stem)
                detections[period_start] = (
                    parse_birdnet_calls(
                        csv_path,
                        min_confidence=self.config.min_confidence,
                        candidate_min_confidence=self.config.candidate_min_confidence,
                    )
                    if csv_path
                    else []
                )
            return detections
        finally:
            if not self.config.keep_work_outputs:
                shutil.rmtree(output_dir, ignore_errors=True)

    def prewarm(self, output_dir: Path, recorded_at: datetime, night: bool) -> None:
        if self.config.use_subprocess or self.config.python:
            return
        output_dir.mkdir(parents=True, exist_ok=True)
        try:
            with tempfile.TemporaryDirectory(prefix="juara-birdnet-prewarm-") as temp_dir:
                input_path = Path(temp_dir) / "silence.wav"
                _write_silence_wav(input_path)
                self._analyze_with_birdnet(input_path, output_dir, birdnet_week(recorded_at), night, timeout=600)
        finally:
            if not self.config.keep_work_outputs:
                shutil.rmtree(output_dir, ignore_errors=True)

    def _prepare_audio_input(self, source: Path, target: Path) -> None:
        gain_db = self.config.audio_gain_db
        if gain_db == 0:
            try:
                os.symlink(source.resolve(), target)
            except OSError:
                shutil.copy2(source, target)
            return
        target.parent.mkdir(parents=True, exist_ok=True)
        command = [
            self.config.ffmpeg_command,
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            str(source),
            "-af",
            f"volume={gain_db}dB,alimiter=limit=0.95",
            "-ac",
            "1",
            "-ar",
            "48000",
            "-sample_fmt",
            "s16",
            str(target),
        ]
        proc = subprocess.run(command, check=False, capture_output=True, text=True, timeout=600)
        if proc.returncode != 0:
            raise RuntimeError((proc.stderr or proc.stdout or f"ffmpeg exited {proc.returncode}").strip())

    def _analyze_with_birdnet(self, input_path: Path, output_dir: Path, week: int, night: bool, timeout: int) -> None:
        if self.config.use_subprocess or self.config.python:
            command = self._command(input_path, output_dir, week, night)
            start = time.monotonic()
            proc = subprocess.run(command, check=False, capture_output=True, text=True, timeout=timeout)
            elapsed = time.monotonic() - start
            LOGGER.info("BirdNET subprocess finished in %.1fs for %s", elapsed, input_path)
            if proc.returncode != 0:
                raise RuntimeError((proc.stderr or proc.stdout or f"BirdNET exited {proc.returncode}").strip())
            return

        start = time.monotonic()
        with self._analyze_lock:
            try:
                self._analyze_inprocess(input_path, output_dir, week, night)
            except Exception:
                LOGGER.exception("In-process BirdNET failed for %s", input_path)
                raise
        LOGGER.info("BirdNET in-process finished in %.1fs for %s", time.monotonic() - start, input_path)

    def _analyze_inprocess(self, input_path: Path, output_dir: Path, week: int, night: bool) -> None:
        output_dir.mkdir(parents=True, exist_ok=True)
        self._patch_birdnet_runtime()
        from birdnet_analyzer.analyze.core import analyze as birdnet_analyze

        species_list_path = self._species_list_path()
        latitude, longitude = self._coordinates()
        birdnet_analyze(
            str(input_path),
            output=str(output_dir),
            lat=-1 if species_list_path else latitude,
            lon=-1 if species_list_path else longitude,
            week=week,
            slist=str(species_list_path) if species_list_path else None,
            sf_thresh=self.config.sf_threshold,
            min_conf=min(self.config.min_confidence, self.config.candidate_min_confidence),
            sensitivity=self.config.sensitivity_night if night else self.config.sensitivity_day,
            overlap=self.config.overlap_night if night else self.config.overlap_day,
            rtype="csv",
            threads=self.config.workers,
            batch_size=self.config.batch_size,
        )

    def _patch_birdnet_runtime(self) -> None:
        if self._inprocess_ready or not self.config.fast_tflite:
            self._inprocess_ready = True
            return
        import numpy as np
        import birdnet_analyzer.config as birdnet_config
        import birdnet_analyzer.model as birdnet_model

        from tensorflow import lite as tflite

        def fast_load_interpreter(model_path, threads):
            return tflite.Interpreter(model_path=model_path, num_threads=threads)

        original_predict = birdnet_model.predict

        def fast_predict(sample):
            if birdnet_config.CUSTOM_CLASSIFIER is not None or birdnet_config.USE_PERCH:
                return original_predict(sample)
            birdnet_model.load_model()
            if birdnet_model.PBMODEL is not None:
                return birdnet_model.PBMODEL.basic(sample)["scores"]

            sample_array = np.asarray(sample, dtype="float32")
            batch_len = len(sample_array)
            model_input = sample_array

            desired_shape = list(model_input.shape)
            if getattr(birdnet_model, "_JUARA_INPUT_SHAPE", None) != desired_shape:
                birdnet_model.INTERPRETER.resize_tensor_input(birdnet_model.INPUT_LAYER_INDEX, desired_shape)
                birdnet_model.INTERPRETER.allocate_tensors()
                birdnet_model._JUARA_INPUT_SHAPE = desired_shape

            birdnet_model.INTERPRETER.set_tensor(
                birdnet_model.INPUT_LAYER_INDEX,
                model_input,
            )
            birdnet_model.INTERPRETER.invoke()
            return birdnet_model.INTERPRETER.get_tensor(birdnet_model.OUTPUT_LAYER_INDEX)[:batch_len]

        birdnet_model._load_interpreter = fast_load_interpreter
        birdnet_model.predict = fast_predict
        self._inprocess_ready = True

    def _command(self, input_path: Path, output_dir: Path, week: int, night: bool) -> list[str]:
        species_list_path = self._species_list_path()
        latitude, longitude = self._coordinates()
        command = [
            self.config.python or sys.executable,
            "-m",
            "birdnet_analyzer.analyze",
            str(input_path),
            "-o",
            str(output_dir),
            "--lat",
            str(-1 if species_list_path else latitude),
            "--lon",
            str(-1 if species_list_path else longitude),
            "--week",
            str(week),
            "--sf_thresh",
            str(self.config.sf_threshold),
            "--min_conf",
            str(min(self.config.min_confidence, self.config.candidate_min_confidence)),
            "--sensitivity",
            str(self.config.sensitivity_night if night else self.config.sensitivity_day),
            "--overlap",
            str(self.config.overlap_night if night else self.config.overlap_day),
            "--rtype",
            "csv",
            "-t",
            str(self.config.workers),
            "-b",
            str(self.config.batch_size),
        ]
        if species_list_path:
            command.extend(["--slist", str(species_list_path)])
        return command

    def _coordinates(self) -> tuple[float, float]:
        return self._latitude, self._longitude

    def _species_list_path(self):
        mode = str(self.config.species_filter_mode or "generated_list").strip().lower()
        if mode in {"generated_list", "generated", "custom_list", "custom"}:
            return self.config.species_list_path
        if mode in {"birdnet_location", "birdnet", "native", "location"}:
            return None
        LOGGER.warning(
            "Unknown birdnet.species_filter_mode=%r; using generated_list behavior",
            self.config.species_filter_mode,
        )
        return self.config.species_list_path


class MockBirdNetRunner(BirdNetRunner):
    def __init__(self) -> None:
        super().__init__(BirdNetConfig(enabled=False), LocationConfig())

    def analyze_audio(self, audio_path: Path, output_dir: Path, recorded_at: datetime, night: bool) -> list[BirdCall]:
        if night:
            return [
                BirdCall(
                    0.0,
                    3.0,
                    (BirdCandidate("Pauraque", 0.61), BirdCandidate("Little nightjar", 0.18)),
                )
            ]
        return [
            BirdCall(
                0.0,
                3.0,
                (BirdCandidate("Hyacinth macaw", 0.72), BirdCandidate("Blue-and-yellow macaw", 0.14)),
            ),
            BirdCall(3.0, 6.0, (BirdCandidate("Hyacinth macaw", 0.68),)),
            BirdCall(6.0, 9.0, (BirdCandidate("Rufous hornero", 0.66),)),
        ]

    def analyze_audio_batch(
        self, jobs: list[BirdNetAudioJob], output_dir: Path, week: int, night: bool
    ) -> dict[datetime, list[BirdCall]]:
        return {job.period_start: self.analyze_audio(job.audio_path, output_dir, job.period_start, night) for job in jobs}


def parse_birdnet_calls(
    csv_path: Path,
    min_confidence: float = 0.25,
    candidate_min_confidence: float = 0.10,
) -> list[BirdCall]:
    grouped: dict[tuple[float | None, float | None, int], list[BirdCandidate]] = defaultdict(list)
    with csv_path.open(newline="") as handle:
        reader = csv.DictReader(handle)
        for row_index, row in enumerate(reader):
            species = _first_present(
                row,
                [
                    "Common name",
                    "Common Name",
                    "common_name",
                    "Species",
                    "species",
                    "Label",
                    "label",
                    "Scientific name",
                    "Scientific Name",
                    "scientific_name",
                ],
            )
            if not species:
                continue
            confidence = _as_float(
                _first_present(row, ["Confidence", "confidence", "Score", "score", "Common name confidence"])
            )
            if confidence is not None and confidence < candidate_min_confidence:
                continue
            grouped[_birdnet_call_key(row, row_index)].append(BirdCandidate(species, confidence))

    calls: list[BirdCall] = []
    for start_seconds, end_seconds, _row_index in grouped:
        candidates = tuple(
            sorted(
                grouped[(start_seconds, end_seconds, _row_index)],
                key=lambda item: (-(item.confidence if item.confidence is not None else -1.0), item.species),
            )
        )
        if not candidates:
            continue
        top_confidence = candidates[0].confidence
        if top_confidence is not None and top_confidence < min_confidence:
            continue
        calls.append(BirdCall(start_seconds, end_seconds, candidates))

    return sorted(
        calls,
        key=lambda call: (-(call.top_candidate.confidence if call.top_candidate and call.top_candidate.confidence else 0.0), call.start_seconds or 0.0),
    )


def parse_birdnet_csv(
    csv_path: Path,
    min_confidence: float = 0.25,
    candidate_min_confidence: float = 0.10,
) -> list[BirdDetection]:
    return calls_to_detections(parse_birdnet_calls(csv_path, min_confidence, candidate_min_confidence))


def _birdnet_call_key(row: dict[str, str], row_index: int) -> tuple[float | None, float | None, int]:
    start_seconds = _as_float(
        _first_present(row, ["Start (s)", "Start", "start", "Begin Time (s)", "Begin Time", "begin_time"])
    )
    end_seconds = _as_float(_first_present(row, ["End (s)", "End", "end", "End Time (s)", "End Time", "end_time"]))
    if start_seconds is None and end_seconds is None:
        return None, None, row_index
    return start_seconds, end_seconds, 0


def birdnet_week(value: datetime) -> int:
    local = value
    week_in_month = min(4, ((local.day - 1) // 7) + 1)
    return (local.month - 1) * 4 + week_in_month


def _write_silence_wav(path: Path, seconds: int = 3, sample_rate: int = 48000) -> None:
    frames = b"\x00\x00" * sample_rate * seconds
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(sample_rate)
        handle.writeframes(frames)


def _latest_csv(output_dir: Path) -> Path | None:
    csvs = sorted(output_dir.glob("*BirdNET.results.csv"), key=lambda path: path.stat().st_mtime, reverse=True)
    if csvs:
        return csvs[0]
    csvs = sorted(output_dir.glob("*.csv"), key=lambda path: path.stat().st_mtime, reverse=True)
    return csvs[0] if csvs else None


def _csv_for_stem(output_dir: Path, stem: str) -> Path | None:
    expected = output_dir / f"{stem}.BirdNET.results.csv"
    if expected.exists():
        return expected
    matches = sorted(output_dir.glob(f"{stem}*.csv"), key=lambda path: path.stat().st_mtime, reverse=True)
    return matches[0] if matches else None


def _first_present(row: dict[str, str], keys: list[str]) -> str | None:
    lowered = {key.lower(): value for key, value in row.items()}
    for key in keys:
        value = row.get(key)
        if value:
            return value.strip()
        value = lowered.get(key.lower())
        if value:
            return value.strip()
    return None


def _as_float(value) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
