"""Resolve faster-whisper paths against the checked-out project root."""

from __future__ import annotations

import os
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_FASTER_WHISPER_CACHE_ROOT = ".tooling/wsl/cache/asr"


def resolve_faster_whisper_cache_root(
    value: str | Path | None = None, *, project_root: Path | None = None
) -> Path:
    """Resolve a cache root independently of the caller's current directory."""
    raw = value if value is not None else os.getenv("FASTER_WHISPER_DOWNLOAD_ROOT")
    if raw is None or not str(raw).strip():
        raw = DEFAULT_FASTER_WHISPER_CACHE_ROOT
    path = Path(raw).expanduser()
    root = (project_root or PROJECT_ROOT).expanduser().resolve()
    if not path.is_absolute():
        path = root / path
    return path.resolve()


def local_faster_whisper_model_path(
    model: str | Path, *, project_root: Path | None = None
) -> Path | None:
    """Return a project-rooted path for local directories, preserving model IDs."""
    raw = os.fspath(model).strip()
    if not raw:
        return None
    path = Path(raw).expanduser()
    root = (project_root or PROJECT_ROOT).expanduser().resolve()
    candidate = path if path.is_absolute() else root / path
    explicitly_path_like = raw in {".", ".."} or raw.startswith(("./", "../", "~"))
    if path.is_absolute() or explicitly_path_like or candidate.exists():
        return candidate.resolve()
    return None


def resolve_faster_whisper_model(
    model: str, *, project_root: Path | None = None
) -> str:
    """Resolve a local model path while leaving aliases and Hub repo IDs intact."""
    local_path = local_faster_whisper_model_path(model, project_root=project_root)
    return str(local_path) if local_path is not None else model


def model_directory_is_complete(path: Path) -> bool:
    """Check the files faster-whisper needs without loading model weights."""
    required_files = ("config.json", "model.bin", "tokenizer.json")
    try:
        return all((path / name).is_file() and (path / name).stat().st_size > 0
                   for name in required_files)
    except OSError:
        return False
