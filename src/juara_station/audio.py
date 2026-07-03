from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
import subprocess
import wave

from .config import AudioConfig, AudioModeConfig
from .storage import utc_now


@dataclass(frozen=True)
class RecordingResult:
    path: Path | None
    started_at: datetime
    ended_at: datetime
    status: str
    error: str | None = None


class AudioRecorder:
    def __init__(self, config: AudioConfig):
        self.config = config
        self._mixer_configured_devices: set[str] = set()

    def record(self, output_path: Path, duration_seconds: int, night: bool) -> RecordingResult:
        started = utc_now()
        output_path.parent.mkdir(parents=True, exist_ok=True)
        mode = self.config.night if night else self.config.day
        devices = [self.config.device]
        errors: list[str] = []
        index = 0
        while index < len(devices):
            device = devices[index]
            index += 1
            self._configure_mixer_once(device)
            command = self._command(output_path, duration_seconds, mode, device)
            try:
                proc = subprocess.run(command, check=False, capture_output=True, text=True, timeout=duration_seconds + 300)
            except (OSError, subprocess.SubprocessError) as exc:
                error = str(exc)
                errors.append(f"{device}: {error}")
                if not _audio_device_missing_error(error):
                    break
                self._append_discovered_capture_devices(devices)
                continue
            if proc.returncode == 0:
                return RecordingResult(output_path, started, utc_now(), "recorded")
            error = (proc.stderr or proc.stdout or f"arecord exited {proc.returncode}").strip()
            errors.append(f"{device}: {error}")
            if not _audio_device_missing_error(error):
                break
            self._append_discovered_capture_devices(devices)
        return RecordingResult(output_path, started, utc_now(), "error", "\n".join(errors) if errors else "arecord failed")

    def _command(self, output_path: Path, duration_seconds: int, mode: AudioModeConfig, device: str) -> list[str]:
        return [
            self.config.record_command,
            "-D",
            device,
            "-f",
            mode.sample_format,
            "-r",
            str(mode.sample_rate),
            "-c",
            str(mode.channels),
            "-d",
            str(duration_seconds),
            "-t",
            "wav",
            str(output_path),
        ]

    def _configure_mixer_once(self, device: str) -> None:
        if device in self._mixer_configured_devices or self.config.capture_gain_percent is None:
            return
        self._mixer_configured_devices.add(device)
        percent = f"{max(0, min(100, self.config.capture_gain_percent))}%"
        for control in self.config.capture_gain_controls:
            try:
                subprocess.run(
                    [self.config.mixer_command, "-D", device, "sset", control, percent],
                    check=False,
                    capture_output=True,
                    text=True,
                    timeout=5,
                )
            except (OSError, subprocess.SubprocessError):
                continue

    def _append_discovered_capture_devices(self, devices: list[str]) -> None:
        for device in self._discover_capture_devices():
            if device not in devices:
                devices.append(device)

    def _discover_capture_devices(self) -> list[str]:
        try:
            proc = subprocess.run(
                [self.config.record_command, "-l"],
                check=False,
                capture_output=True,
                text=True,
                timeout=5,
            )
        except (OSError, subprocess.SubprocessError):
            return []
        if proc.returncode != 0:
            return []
        discovered: list[str] = []
        for line in proc.stdout.splitlines():
            parsed = _parse_arecord_hardware_line(line)
            if parsed and parsed not in discovered:
                discovered.append(parsed)
        return discovered


def _parse_arecord_hardware_line(line: str) -> str | None:
    text = line.strip()
    if not text.startswith("card ") or " device " not in text:
        return None
    try:
        card_text = text.split(":", 1)[0].removeprefix("card ").strip()
        device_text = text.split(" device ", 1)[1].split(":", 1)[0].strip()
        card = int(card_text)
        device = int(device_text)
    except (IndexError, ValueError):
        return None
    return f"plughw:{card},{device}"


def _audio_device_missing_error(error: str) -> bool:
    text = error.lower()
    return any(
        token in text
        for token in (
            "cannot get card index",
            "no such device",
            "unknown pcm",
            "device or resource busy",
        )
    )


class MockAudioRecorder(AudioRecorder):
    def __init__(self) -> None:
        super().__init__(AudioConfig(enabled=False))

    def record(self, output_path: Path, duration_seconds: int, night: bool) -> RecordingResult:
        started = utc_now()
        output_path.parent.mkdir(parents=True, exist_ok=True)
        sample_rate = 24000 if night else 48000
        frames = min(duration_seconds, 2) * sample_rate
        with wave.open(str(output_path), "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(sample_rate)
            wav.writeframes(b"\x00\x00" * frames)
        return RecordingResult(output_path, started, utc_now(), "recorded")
