#!/usr/bin/env bash
set -euo pipefail

VENV_PYTHON="${VENV_PYTHON:-/opt/juara-wildlife-station/.venv/bin/python}"
AUDIO_ROOT="${AUDIO_ROOT:-/tmp/juara-audio}"
BENCH_DIR="${BENCH_DIR:-/tmp/juara_birdnet_chunk_bench}"
DURATION_SECONDS="${DURATION_SECONDS:-12}"
CHUNK_SECONDS="${CHUNK_SECONDS:-3}"
LATITUDE="${LATITUDE:--16.68260}"
LONGITUDE="${LONGITUDE:--56.90453}"
WEEK="${WEEK:-19}"
SOURCE_AUDIO="${1:-}"

if [[ -z "$SOURCE_AUDIO" ]]; then
  SOURCE_AUDIO="$(find "$AUDIO_ROOT" -type f -name '*.wav' | sort | tail -1)"
fi
if [[ -z "$SOURCE_AUDIO" || ! -f "$SOURCE_AUDIO" ]]; then
  echo "No source WAV found." >&2
  exit 1
fi

FFMPEG="$(command -v ffmpeg)"
rm -rf "$BENCH_DIR"
mkdir -p "$BENCH_DIR/chunks" "$BENCH_DIR/out_normal" "$BENCH_DIR/out_chunks"

echo "source=$SOURCE_AUDIO"
"$FFMPEG" -y -hide_banner -loglevel error -t "$DURATION_SECONDS" -i "$SOURCE_AUDIO" -c copy "$BENCH_DIR/sample.wav"
"$FFMPEG" -y -hide_banner -loglevel error -i "$BENCH_DIR/sample.wav" \
  -f segment -segment_time "$CHUNK_SECONDS" -c copy "$BENCH_DIR/chunks/chunk_%03d.wav"

python3 - "$BENCH_DIR/sample.wav" "$BENCH_DIR/chunks" <<'PY'
import sys
import wave
from pathlib import Path

sample = Path(sys.argv[1])
chunks = sorted(Path(sys.argv[2]).glob("*.wav"))

def info(path: Path) -> tuple[float, int, int, int]:
    with wave.open(str(path), "rb") as wav:
        return (
            round(wav.getnframes() / wav.getframerate(), 3),
            wav.getframerate(),
            wav.getnchannels(),
            wav.getsampwidth(),
        )

print("sample_info=", info(sample))
print("chunk_count=", len(chunks))
for chunk in chunks:
    print("chunk_info=", chunk.name, info(chunk))
PY

common=(
  --lat "$LATITUDE"
  --lon "$LONGITUDE"
  --week "$WEEK"
  --sf_thresh 0.03
  --min_conf 0.25
  --sensitivity 0.8
  --overlap 0.0
  --rtype csv
  -t 1
  -b 1
)

start="$(date +%s)"
echo "normal_start=$(date -Iseconds)"
"$VENV_PYTHON" -m birdnet_analyzer.analyze "$BENCH_DIR/sample.wav" \
  -o "$BENCH_DIR/out_normal" "${common[@]}" >"$BENCH_DIR/normal.log" 2>&1
end="$(date +%s)"
echo "normal_seconds=$((end - start))"

start="$(date +%s)"
echo "chunked_start=$(date -Iseconds)"
"$VENV_PYTHON" -m birdnet_analyzer.analyze "$BENCH_DIR/chunks" \
  -o "$BENCH_DIR/out_chunks" "${common[@]}" >"$BENCH_DIR/chunked.log" 2>&1
end="$(date +%s)"
echo "chunked_seconds=$((end - start))"

echo "normal_log_tail"
tail -20 "$BENCH_DIR/normal.log"
echo "chunked_log_tail"
tail -20 "$BENCH_DIR/chunked.log"

echo "normal_outputs"
find "$BENCH_DIR/out_normal" -maxdepth 2 -type f -printf '%s %p\n' | sort
echo "chunked_outputs"
find "$BENCH_DIR/out_chunks" -maxdepth 2 -type f -printf '%s %p\n' | sort

echo "normal_results_preview"
find "$BENCH_DIR/out_normal" -name '*results.csv' -print -exec sed -n '1,12p' {} \;
echo "chunked_results_preview"
find "$BENCH_DIR/out_chunks" -name '*results.csv' -print -exec sed -n '1,12p' {} \;
