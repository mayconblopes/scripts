"""Transcribe the requested Japanese video locally with faster-whisper."""

from __future__ import annotations

import json
import sys
from pathlib import Path


def main() -> int:
    if len(sys.argv) == 2 and sys.argv[1] in {"-h", "--help"}:
        print("uso: transcribe_video.py VIDEO SAIDA_JSON")
        return 0
    if len(sys.argv) != 3:
        print("uso: transcribe_video.py VIDEO SAIDA_JSON", file=sys.stderr)
        return 2

    video = Path(sys.argv[1])
    output = Path(sys.argv[2])
    deps = Path(__file__).with_name(".transcribe_deps")
    model_dir = Path(__file__).with_name(".whisper_models")
    sys.path.insert(0, str(deps))

    from faster_whisper import WhisperModel

    model = WhisperModel(
        "base",
        device="cpu",
        compute_type="int8",
        download_root=str(model_dir),
    )
    segments, info = model.transcribe(
        str(video),
        language="ja",
        beam_size=5,
        best_of=5,
        temperature=0.0,
        vad_filter=True,
        condition_on_previous_text=False,
        initial_prompt=(
            "ジーグル、Jiegle、トリプルオカリナ、プラスチック製、アルトC調、"
            "ナイトオカリナ、楽天"
        ),
        word_timestamps=False,
    )
    rows = [
        {"start": round(segment.start, 3), "end": round(segment.end, 3), "text": segment.text.strip()}
        for segment in segments
        if segment.text.strip()
    ]
    output.write_text(
        json.dumps(
            {
                "language": info.language,
                "language_probability": info.language_probability,
                "duration": info.duration,
                "segments": rows,
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    print(f"{len(rows)} segmentos; idioma={info.language}; duração={info.duration:.3f}s")
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
