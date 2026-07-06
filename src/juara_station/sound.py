from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import csv
import json
import os
import subprocess
import sys

from .config import YamNetConfig
from .storage import SoundDetection


YAMNET_SOURCE = "yamnet"

YAMNET_CATEGORY_TERMS: dict[str, tuple[str, ...]] = {
    "bird": ("bird", "chirp", "tweet", "squawk", "caw", "hoot", "coo"),
    "frog": ("frog", "croak"),
    "primate": ("monkey", "primate"),
    "vehicle": ("vehicle", "car", "truck", "bus", "motor vehicle", "traffic"),
    "motorcycle": ("motorcycle",),
    "chainsaw": ("chainsaw",),
    "wind": ("wind",),
    "rain": ("rain", "thunderstorm", "waterfall"),
    "human": ("speech", "conversation", "shout", "yell", "human voice", "laughter"),
    "insect": ("insect", "cricket", "cicada"),
}


@dataclass(frozen=True)
class YamNetSummary:
    detections: list[SoundDetection]
    category_scores: dict[str, float]


class YamNetRunner:
    def __init__(self, config: YamNetConfig):
        self.config = config
        self._labels: list[str] | None = None
        self._saved_model = None
        self._tflite_interpreter = None
        self._tflite_input_index: int | None = None
        self._tflite_output_index: int | None = None

    def analyze_audio(self, audio_path: Path) -> YamNetSummary:
        if not self.config.enabled:
            return YamNetSummary([], {})
        model_path = Path(self.config.model_path).expanduser() if self.config.model_path else None
        if model_path is None or not model_path.exists():
            raise RuntimeError("YAMNet model_path is not configured or does not exist")
        if self.config.python and Path(self.config.python).expanduser() != Path(sys.executable):
            return self._analyze_subprocess(audio_path, model_path)

        labels = self._class_labels()
        waveform = _load_audio_as_16khz_float32(
            audio_path,
            self.config.ffmpeg_command,
            max_audio_seconds=self.config.max_audio_seconds,
        )
        if model_path.suffix == ".tflite":
            scores = self._analyze_tflite(model_path, waveform)
        else:
            scores = self._analyze_saved_model(model_path, waveform)
        detections = _scores_to_detections(scores, labels, self.config)
        categories = category_scores(detections)
        return YamNetSummary(detections=detections, category_scores=categories)

    def _analyze_subprocess(self, audio_path: Path, model_path: Path) -> YamNetSummary:
        command = [
            str(Path(self.config.python or sys.executable).expanduser()),
            "-m",
            "juara_station.yamnet_worker",
            "--audio",
            str(audio_path),
            "--model",
            str(model_path),
            "--class-map",
            str(Path(self.config.class_map_path or "").expanduser()),
            "--ffmpeg",
            self.config.ffmpeg_command,
            "--min-confidence",
            str(self.config.min_confidence),
            "--top-k",
            str(self.config.top_k),
            "--max-audio-seconds",
            str(self.config.max_audio_seconds),
        ]
        env = os.environ.copy()
        source_root = str(Path(__file__).resolve().parents[1])
        env["PYTHONPATH"] = source_root + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
        proc = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            timeout=max(30, int(self.config.subprocess_timeout_seconds)),
            env=env,
        )
        if proc.returncode != 0:
            message = (proc.stderr or proc.stdout or f"YAMNet subprocess exited {proc.returncode}").strip()
            raise RuntimeError(message)
        payload = json.loads(proc.stdout)
        detections = [
            SoundDetection(item["label"], float(item["score"]), source=YAMNET_SOURCE, category=item.get("category"))
            for item in payload.get("detections", [])
        ]
        return YamNetSummary(detections=detections, category_scores=category_scores(detections))

    def _class_labels(self) -> list[str]:
        if self._labels is not None:
            return self._labels
        if not self.config.class_map_path:
            raise RuntimeError("YAMNet class_map_path is not configured")
        path = Path(self.config.class_map_path).expanduser()
        if not path.exists():
            raise RuntimeError(f"YAMNet class map does not exist: {path}")
        with path.open(newline="") as handle:
            rows = list(csv.DictReader(handle))
        labels = []
        for row in sorted(rows, key=lambda item: int(item.get("index") or len(labels))):
            labels.append(row.get("display_name") or row.get("label") or row.get("name") or "")
        self._labels = labels
        return labels

    def _analyze_saved_model(self, model_path: Path, waveform):
        import numpy as np
        import tensorflow as tf

        if self._saved_model is None:
            self._saved_model = tf.saved_model.load(str(model_path))
        scores, _embeddings, _spectrogram = self._saved_model(waveform)
        array = scores.numpy() if hasattr(scores, "numpy") else np.asarray(scores)
        if array.ndim == 1:
            return array
        return array.max(axis=0)

    def _analyze_tflite(self, model_path: Path, waveform):
        import numpy as np

        if self._tflite_interpreter is None:
            try:
                from tflite_runtime.interpreter import Interpreter
            except ImportError:
                from tensorflow.lite import Interpreter

            interpreter = Interpreter(model_path=str(model_path))
            input_details = interpreter.get_input_details()
            waveform = np.asarray(waveform, dtype=np.float32)
            target_shape = list(input_details[0]["shape"])
            expected_samples = _tflite_expected_samples(input_details[0])
            if not expected_samples and (-1 in target_shape or target_shape != list(waveform.shape)):
                try:
                    interpreter.resize_tensor_input(input_details[0]["index"], waveform.shape, strict=False)
                except TypeError:
                    interpreter.resize_tensor_input(input_details[0]["index"], waveform.shape)
            interpreter.allocate_tensors()
            output_details = interpreter.get_output_details()
            self._tflite_interpreter = interpreter
            self._tflite_input_index = input_details[0]["index"]
            self._tflite_output_index = _score_output_index(output_details)

        interpreter = self._tflite_interpreter
        input_details = interpreter.get_input_details()
        expected_samples = _tflite_expected_samples(input_details[0])
        if expected_samples:
            frame_scores = []
            input_shape = [int(value) for value in input_details[0].get("shape")]
            for frame in _fixed_length_frames(np.asarray(waveform, dtype=np.float32), expected_samples):
                tensor = frame.reshape(input_shape) if len(input_shape) == 2 else frame
                interpreter.set_tensor(self._tflite_input_index, tensor)
                interpreter.invoke()
                frame_scores.append(interpreter.get_tensor(self._tflite_output_index))
            scores = np.asarray(frame_scores)
        else:
            interpreter.set_tensor(self._tflite_input_index, waveform)
            interpreter.invoke()
            scores = interpreter.get_tensor(self._tflite_output_index)
        if scores.ndim == 1:
            return scores
        return scores.max(axis=tuple(range(scores.ndim - 1)))


class MockYamNetRunner(YamNetRunner):
    def __init__(self) -> None:
        super().__init__(YamNetConfig(enabled=True))

    def analyze_audio(self, audio_path: Path) -> YamNetSummary:
        detections = [
            SoundDetection("Bird vocalization, bird call, bird song", 0.81, source=YAMNET_SOURCE, category="bird"),
            SoundDetection("Frog", 0.32, source=YAMNET_SOURCE, category="frog"),
        ]
        return YamNetSummary(detections=detections, category_scores=category_scores(detections))


def category_scores(detections: list[SoundDetection]) -> dict[str, float]:
    scores = {category: 0.0 for category in YAMNET_CATEGORY_TERMS}
    for detection in detections:
        label = detection.label.casefold()
        for category, terms in YAMNET_CATEGORY_TERMS.items():
            if any(term in label for term in terms):
                scores[category] = max(scores[category], float(detection.score or 0.0))
    return scores


def _scores_to_detections(scores, labels: list[str], config: YamNetConfig) -> list[SoundDetection]:
    import numpy as np

    values = np.asarray(scores, dtype=float)
    top_k = max(1, int(config.top_k))
    min_score = max(0.0, float(config.min_confidence))
    ranked = sorted(enumerate(values.tolist()), key=lambda item: (-item[1], item[0]))
    detections: list[SoundDetection] = []
    for index, score in ranked:
        if score < min_score:
            continue
        label = labels[index] if index < len(labels) and labels[index] else f"class_{index}"
        detections.append(SoundDetection(label, score, source=YAMNET_SOURCE, category=_category_for_label(label)))
        if len(detections) >= top_k:
            break
    return detections


def _category_for_label(label: str) -> str | None:
    lower = label.casefold()
    for category, terms in YAMNET_CATEGORY_TERMS.items():
        if any(term in lower for term in terms):
            return category
    return None


def _score_output_index(output_details) -> int:
    for detail in output_details:
        shape_value = detail.get("shape")
        shape = list(shape_value) if shape_value is not None else []
        if shape and shape[-1] == 521:
            return detail["index"]
    return output_details[0]["index"]


def _tflite_expected_samples(input_detail) -> int | None:
    shape_value = input_detail.get("shape")
    shape = [int(value) for value in shape_value] if shape_value is not None else []
    if len(shape) == 1 and shape[0] > 0:
        return shape[0]
    if len(shape) == 2 and shape[0] == 1 and shape[1] > 0:
        return shape[1]
    return None


def _fixed_length_frames(waveform, expected_samples: int):
    import numpy as np

    waveform = np.asarray(waveform, dtype=np.float32)
    if waveform.size < expected_samples:
        yield np.pad(waveform, (0, expected_samples - waveform.size))
        return
    step = max(1, expected_samples // 2)
    for start in range(0, waveform.size - expected_samples + 1, step):
        yield waveform[start : start + expected_samples]
    remainder = waveform.size % step
    if remainder and waveform.size > expected_samples:
        yield waveform[-expected_samples:]


def _load_audio_as_16khz_float32(audio_path: Path, ffmpeg_command: str, max_audio_seconds: int | None = None):
    import numpy as np

    command = [
        ffmpeg_command,
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        str(audio_path),
        "-ac",
        "1",
        "-ar",
        "16000",
    ]
    if max_audio_seconds and max_audio_seconds > 0:
        command.extend(["-t", str(max_audio_seconds)])
    command.extend(["-f", "f32le", "-"])
    proc = subprocess.run(
        command,
        check=False,
        capture_output=True,
        timeout=300,
    )
    if proc.returncode != 0:
        raise RuntimeError((proc.stderr.decode(errors="ignore") or f"ffmpeg exited {proc.returncode}").strip())
    waveform = np.frombuffer(proc.stdout, dtype=np.float32)
    if waveform.size == 0:
        raise RuntimeError(f"YAMNet audio decode produced no samples: {audio_path}")
    return waveform
