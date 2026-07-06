from __future__ import annotations

import argparse
import json
from pathlib import Path

from .config import YamNetConfig
from .sound import YamNetRunner


def main() -> int:
    parser = argparse.ArgumentParser(description="Run YAMNet in an isolated Python environment.")
    parser.add_argument("--audio", required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--class-map", required=True)
    parser.add_argument("--ffmpeg", default="ffmpeg")
    parser.add_argument("--min-confidence", type=float, default=0.15)
    parser.add_argument("--top-k", type=int, default=8)
    parser.add_argument("--max-audio-seconds", type=int, default=30)
    args = parser.parse_args()

    config = YamNetConfig(
        enabled=True,
        model_path=args.model,
        class_map_path=args.class_map,
        ffmpeg_command=args.ffmpeg,
        min_confidence=args.min_confidence,
        top_k=args.top_k,
        max_audio_seconds=args.max_audio_seconds,
    )
    summary = YamNetRunner(config).analyze_audio(Path(args.audio))
    print(
        json.dumps(
            {
                "detections": [
                    {
                        "label": detection.label,
                        "score": detection.score,
                        "category": detection.category,
                    }
                    for detection in summary.detections
                ],
                "category_scores": summary.category_scores,
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
