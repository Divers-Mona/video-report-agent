"""Prepare configured faster-whisper weights without transcribing audio."""

from __future__ import annotations

import os

from dotenv import load_dotenv
from faster_whisper import WhisperModel

from video_report_agent.asr_paths import (
    PROJECT_ROOT,
    local_faster_whisper_model_path,
    resolve_faster_whisper_cache_root,
)

load_dotenv(PROJECT_ROOT / ".env")


def main() -> int:
    model_name = (os.getenv("ASR_MODEL") or "small").strip()
    cache_root = resolve_faster_whisper_cache_root(project_root=PROJECT_ROOT)
    cache_root.mkdir(parents=True, exist_ok=True)

    model_path = local_faster_whisper_model_path(model_name, project_root=PROJECT_ROOT)
    if model_path is not None:
        model_name = str(model_path)
        if not model_path.is_dir() or not (model_path / "model.bin").is_file():
            raise SystemExit("ASR_MODEL points to a directory without model.bin")

    print(
        f"Preparing faster-whisper weights for {model_name} "
        "with CPU int8; no audio inference will run."
    )

    WhisperModel(
        model_name,
        device="cpu",
        compute_type="int8",
        download_root=str(cache_root),
    )
    print("faster-whisper weights are ready in the configured local cache.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
