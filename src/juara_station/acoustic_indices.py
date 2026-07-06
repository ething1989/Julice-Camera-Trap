from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import math
import wave


ACOUSTIC_INDEX_VERSION = "juara_acoustic_indices_v1"
ACOUSTIC_INDEX_COLUMNS = (
    "acoustic_index_version",
    "acoustic_duration_s",
    "acoustic_sample_rate_hz",
    "acoustic_n_fft",
    "acoustic_hop_length",
    "acoustic_fmin_hz",
    "acoustic_fmax_hz",
    "acoustic_db_threshold",
    "acoustic_activity",
    "acoustic_aci",
    "acoustic_adi",
    "acoustic_aei",
    "acoustic_bioacoustic_index",
    "acoustic_ndsi",
    "acoustic_ndsi_anthrophony",
    "acoustic_ndsi_biophony",
    "acoustic_entropy_h",
    "acoustic_entropy_ht",
    "acoustic_entropy_hf",
    "acoustic_rms",
    "acoustic_index_error",
)

ACOUSTIC_INDEX_SQL_TYPES = {
    "acoustic_index_version": "TEXT",
    "acoustic_duration_s": "REAL",
    "acoustic_sample_rate_hz": "INTEGER",
    "acoustic_n_fft": "INTEGER",
    "acoustic_hop_length": "INTEGER",
    "acoustic_fmin_hz": "REAL",
    "acoustic_fmax_hz": "REAL",
    "acoustic_db_threshold": "REAL",
    "acoustic_activity": "REAL",
    "acoustic_aci": "REAL",
    "acoustic_adi": "REAL",
    "acoustic_aei": "REAL",
    "acoustic_bioacoustic_index": "REAL",
    "acoustic_ndsi": "REAL",
    "acoustic_ndsi_anthrophony": "REAL",
    "acoustic_ndsi_biophony": "REAL",
    "acoustic_entropy_h": "REAL",
    "acoustic_entropy_ht": "REAL",
    "acoustic_entropy_hf": "REAL",
    "acoustic_rms": "REAL",
    "acoustic_index_error": "TEXT",
}


@dataclass(frozen=True)
class AcousticIndexResult:
    acoustic_index_version: str = ACOUSTIC_INDEX_VERSION
    acoustic_duration_s: float | None = None
    acoustic_sample_rate_hz: int | None = None
    acoustic_n_fft: int | None = None
    acoustic_hop_length: int | None = None
    acoustic_fmin_hz: float | None = None
    acoustic_fmax_hz: float | None = None
    acoustic_db_threshold: float | None = None
    acoustic_activity: float | None = None
    acoustic_aci: float | None = None
    acoustic_adi: float | None = None
    acoustic_aei: float | None = None
    acoustic_bioacoustic_index: float | None = None
    acoustic_ndsi: float | None = None
    acoustic_ndsi_anthrophony: float | None = None
    acoustic_ndsi_biophony: float | None = None
    acoustic_entropy_h: float | None = None
    acoustic_entropy_ht: float | None = None
    acoustic_entropy_hf: float | None = None
    acoustic_rms: float | None = None
    acoustic_index_error: str | None = None

    @classmethod
    def from_error(cls, error: str) -> "AcousticIndexResult":
        return cls(acoustic_index_error=_short_error(error))

    def as_db_values(self) -> tuple[object, ...]:
        return tuple(getattr(self, column) for column in ACOUSTIC_INDEX_COLUMNS)


def calculate_acoustic_indices(
    audio_path: Path,
    *,
    n_fft: int = 1024,
    hop_length: int = 512,
    fmin_hz: float = 0.0,
    fmax_hz: float = 10000.0,
    db_threshold: float = -50.0,
    adi_bin_hz: float = 1000.0,
    anthrophony_band_hz: tuple[float, float] = (1000.0, 2000.0),
    biophony_band_hz: tuple[float, float] = (2000.0, 8000.0),
    bioacoustic_band_hz: tuple[float, float] = (2000.0, 8000.0),
    batch_frames: int = 512,
) -> AcousticIndexResult:
    """Calculate common soundscape indices from a WAV recording.

    The defaults follow common soundecology-style conventions: 1 kHz ADI/AEI
    bins with a -50 dBFS activity threshold, 1-2 kHz anthrophony, 2-8 kHz
    biophony, and 2-8 kHz Bioacoustic Index.
    """

    np = _load_numpy()
    samples, sample_rate_hz, duration_s = _read_wav_mono(audio_path, np)
    if samples.size == 0:
        return AcousticIndexResult.from_error("WAV file contained no samples")

    n_fft = max(64, int(n_fft))
    hop_length = max(1, int(hop_length))
    nyquist_hz = sample_rate_hz / 2.0
    fmin_hz = max(0.0, float(fmin_hz))
    fmax_hz = min(float(fmax_hz), nyquist_hz)
    if fmax_hz <= fmin_hz:
        return AcousticIndexResult.from_error("No acoustic index frequency range is available for this sample rate")

    freqs = np.fft.rfftfreq(n_fft, d=1.0 / sample_rate_hz).astype(np.float32)
    analysis_mask = (freqs >= fmin_hz) & (freqs <= fmax_hz)
    if not bool(np.any(analysis_mask)):
        return AcousticIndexResult.from_error("No FFT bins fell inside the acoustic index frequency range")
    analysis_freqs = freqs[analysis_mask]

    bin_masks = _frequency_bin_masks(np, analysis_freqs, fmin_hz, fmax_hz, adi_bin_hz)
    anthro_mask = _band_mask(np, analysis_freqs, anthrophony_band_hz[0], min(anthrophony_band_hz[1], fmax_hz))
    bio_mask = _band_mask(np, analysis_freqs, biophony_band_hz[0], min(biophony_band_hz[1], fmax_hz))
    bioacoustic_mask = _band_mask(np, analysis_freqs, bioacoustic_band_hz[0], min(bioacoustic_band_hz[1], fmax_hz))

    analysis_bins = int(np.count_nonzero(analysis_mask))
    bin_active = np.zeros(len(bin_masks), dtype=np.float64)
    bin_total = np.zeros(len(bin_masks), dtype=np.float64)
    aci_diff = np.zeros(analysis_bins, dtype=np.float64)
    aci_sum = np.zeros(analysis_bins, dtype=np.float64)
    mean_spectrum_sum = np.zeros(analysis_bins, dtype=np.float64)
    temporal_energy_batches = []
    previous_frame = None
    total_active_cells = 0
    total_cells = 0
    frame_count = 0
    anthrophony_energy = 0.0
    biophony_energy = 0.0
    eps = 1e-12

    window = np.hanning(n_fft).astype(np.float32)
    window_scale = max(float(window.sum()) / 2.0, eps)
    for frames in _iter_windowed_frames(np, samples, n_fft, hop_length, batch_frames, window):
        magnitudes = (np.abs(np.fft.rfft(frames, axis=1)).T / window_scale).astype(np.float32, copy=False)
        analysis_magnitudes = magnitudes[analysis_mask, :].astype(np.float64, copy=False)
        if analysis_magnitudes.size == 0:
            continue

        decibels = 20.0 * np.log10(np.maximum(analysis_magnitudes, eps))
        active = decibels > db_threshold
        total_active_cells += int(active.sum())
        total_cells += int(active.size)

        for index, mask in enumerate(bin_masks):
            if bool(np.any(mask)):
                bin_values = decibels[mask, :]
                bin_active[index] += float((bin_values > db_threshold).sum())
                bin_total[index] += float(bin_values.size)

        if previous_frame is not None:
            aci_diff += np.abs(analysis_magnitudes[:, 0] - previous_frame)
        if analysis_magnitudes.shape[1] > 1:
            aci_diff += np.abs(np.diff(analysis_magnitudes, axis=1)).sum(axis=1)
        aci_sum += analysis_magnitudes.sum(axis=1)
        previous_frame = analysis_magnitudes[:, -1].copy()

        mean_spectrum_sum += analysis_magnitudes.sum(axis=1)
        temporal_energy_batches.append(analysis_magnitudes.sum(axis=0))
        if bool(np.any(anthro_mask)):
            anthrophony_energy += float(np.square(analysis_magnitudes[anthro_mask, :]).sum())
        if bool(np.any(bio_mask)):
            biophony_energy += float(np.square(analysis_magnitudes[bio_mask, :]).sum())
        frame_count += analysis_magnitudes.shape[1]

    if frame_count == 0:
        return AcousticIndexResult.from_error("No acoustic index frames could be calculated")

    occupancy = np.divide(bin_active, bin_total, out=np.zeros_like(bin_active), where=bin_total > 0)
    mean_spectrum = mean_spectrum_sum / frame_count
    temporal_energy = np.concatenate(temporal_energy_batches) if temporal_energy_batches else np.array([], dtype=np.float64)
    aci = float(np.divide(aci_diff, aci_sum, out=np.zeros_like(aci_diff), where=aci_sum > 0).sum())
    ndsi_denominator = biophony_energy + anthrophony_energy
    ndsi = (biophony_energy - anthrophony_energy) / ndsi_denominator if ndsi_denominator > 0 else None
    entropy_ht = _normalized_entropy(np, temporal_energy)
    entropy_hf = _normalized_entropy(np, mean_spectrum)
    entropy_h = entropy_ht * entropy_hf if entropy_ht is not None and entropy_hf is not None else None

    return AcousticIndexResult(
        acoustic_duration_s=_clean_float(duration_s),
        acoustic_sample_rate_hz=int(sample_rate_hz),
        acoustic_n_fft=n_fft,
        acoustic_hop_length=hop_length,
        acoustic_fmin_hz=_clean_float(fmin_hz),
        acoustic_fmax_hz=_clean_float(fmax_hz),
        acoustic_db_threshold=_clean_float(db_threshold),
        acoustic_activity=_clean_float(total_active_cells / total_cells if total_cells else None),
        acoustic_aci=_clean_float(aci),
        acoustic_adi=_clean_float(_shannon_index(np, occupancy)),
        acoustic_aei=_clean_float(_gini(np, occupancy)),
        acoustic_bioacoustic_index=_clean_float(
            _bioacoustic_index(np, mean_spectrum, analysis_freqs, bioacoustic_mask, sample_rate_hz, n_fft)
        ),
        acoustic_ndsi=_clean_float(ndsi),
        acoustic_ndsi_anthrophony=_clean_float(anthrophony_energy),
        acoustic_ndsi_biophony=_clean_float(biophony_energy),
        acoustic_entropy_h=_clean_float(entropy_h),
        acoustic_entropy_ht=_clean_float(entropy_ht),
        acoustic_entropy_hf=_clean_float(entropy_hf),
        acoustic_rms=_clean_float(float(np.sqrt(np.mean(np.square(samples, dtype=np.float64))))),
    )


def _load_numpy():
    try:
        import numpy as np
    except ImportError as exc:
        raise RuntimeError("numpy is required to calculate acoustic indices") from exc
    return np


def _read_wav_mono(path: Path, np):
    with wave.open(str(path), "rb") as handle:
        channels = handle.getnchannels()
        sample_width = handle.getsampwidth()
        sample_rate_hz = handle.getframerate()
        frame_count = handle.getnframes()
        frames = handle.readframes(frame_count)
    if frame_count <= 0 or not frames:
        return np.array([], dtype=np.float32), sample_rate_hz, 0.0
    samples = _decode_pcm(np, frames, sample_width)
    if channels > 1:
        usable = (samples.size // channels) * channels
        samples = samples[:usable].reshape(-1, channels).mean(axis=1)
    return samples.astype(np.float32, copy=False), sample_rate_hz, frame_count / float(sample_rate_hz)


def _decode_pcm(np, frames: bytes, sample_width: int):
    if sample_width == 1:
        return (np.frombuffer(frames, dtype=np.uint8).astype(np.float32) - 128.0) / 128.0
    if sample_width == 2:
        return np.frombuffer(frames, dtype="<i2").astype(np.float32) / 32768.0
    if sample_width == 3:
        raw = np.frombuffer(frames, dtype=np.uint8)
        usable = (raw.size // 3) * 3
        triples = raw[:usable].reshape(-1, 3).astype(np.int32)
        values = triples[:, 0] | (triples[:, 1] << 8) | (triples[:, 2] << 16)
        values = np.where(values & 0x800000, values | ~0xFFFFFF, values)
        return values.astype(np.float32) / 8388608.0
    if sample_width == 4:
        return np.frombuffer(frames, dtype="<i4").astype(np.float32) / 2147483648.0
    raise ValueError(f"Unsupported WAV sample width: {sample_width} bytes")


def _iter_windowed_frames(np, samples, n_fft: int, hop_length: int, batch_frames: int, window):
    frame_count = max(1, int(math.ceil(max(0, samples.size - n_fft) / hop_length)) + 1)
    padded_size = (frame_count - 1) * hop_length + n_fft
    if padded_size > samples.size:
        samples = np.pad(samples, (0, padded_size - samples.size))
    batch_frames = max(1, int(batch_frames))
    for frame_start in range(0, frame_count, batch_frames):
        count = min(batch_frames, frame_count - frame_start)
        sample_start = frame_start * hop_length
        sample_end = sample_start + (count - 1) * hop_length + n_fft
        segment = samples[sample_start:sample_end]
        shape = (count, n_fft)
        strides = (samples.strides[0] * hop_length, samples.strides[0])
        frames = np.lib.stride_tricks.as_strided(segment, shape=shape, strides=strides)
        yield frames * window


def _frequency_bin_masks(np, freqs, fmin_hz: float, fmax_hz: float, bin_hz: float):
    bin_hz = max(1.0, float(bin_hz))
    edges = list(np.arange(fmin_hz, fmax_hz, bin_hz, dtype=float))
    if not edges or edges[0] > fmin_hz:
        edges.insert(0, fmin_hz)
    if edges[-1] < fmax_hz:
        edges.append(fmax_hz)
    masks = []
    for low, high in zip(edges, edges[1:]):
        masks.append((freqs >= low) & (freqs < high))
    return masks


def _band_mask(np, freqs, low_hz: float, high_hz: float):
    if high_hz <= low_hz:
        return np.zeros(freqs.shape, dtype=bool)
    return (freqs >= low_hz) & (freqs < high_hz)


def _shannon_index(np, values) -> float | None:
    positive = values[values > 0]
    total = float(positive.sum())
    if total <= 0:
        return None
    proportions = positive / total
    return float(-(proportions * np.log(proportions)).sum())


def _gini(np, values) -> float | None:
    if values.size == 0:
        return None
    sorted_values = np.sort(values.astype(np.float64, copy=False))
    total = float(sorted_values.sum())
    if total <= 0:
        return None
    index = np.arange(1, sorted_values.size + 1, dtype=np.float64)
    return float((2.0 * np.sum(index * sorted_values)) / (sorted_values.size * total) - (sorted_values.size + 1.0) / sorted_values.size)


def _bioacoustic_index(np, mean_spectrum, freqs, mask, sample_rate_hz: int, n_fft: int) -> float | None:
    if not bool(np.any(mask)):
        return None
    eps = 1e-12
    band_db = 20.0 * np.log10(np.maximum(mean_spectrum[mask], eps))
    if band_db.size == 0:
        return None
    baseline = float(band_db.min())
    freq_step_khz = (sample_rate_hz / n_fft) / 1000.0
    return float(np.maximum(band_db - baseline, 0.0).sum() * freq_step_khz)


def _normalized_entropy(np, values) -> float | None:
    positive = values[values > 0]
    total = float(positive.sum())
    if total <= 0 or positive.size <= 1:
        return None
    probabilities = positive / total
    return float(-(probabilities * np.log(probabilities)).sum() / math.log(positive.size))


def _clean_float(value: float | None) -> float | None:
    if value is None:
        return None
    value = float(value)
    if not math.isfinite(value):
        return None
    return value


def _short_error(error: str) -> str:
    error = " ".join(str(error).split())
    return error[:500]
