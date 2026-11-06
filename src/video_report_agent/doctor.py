"""Non-inference checks for the local WSL command-line deployment."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tempfile
import wave
from importlib.metadata import requires as package_requires
from pathlib import Path

from .asr import DEFAULT_FASTER_WHISPER_MODEL
from .asr_paths import (
    resolve_cached_faster_whisper_model,
    resolve_faster_whisper_cache_root,
)
from .local_models import LocalModelError, discover_models

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _tool_check(name: str, arguments: tuple[str, ...] = ("--version",)) -> tuple[bool, str]:
    executable = shutil.which(name)
    if not executable:
        return False, "not found on PATH"
    try:
        result = subprocess.run(
            [executable, *arguments],
            capture_output=True,
            text=True,
            timeout=8,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, f"could not start ({type(exc).__name__})"
    if result.returncode:
        return False, f"exited with status {result.returncode}"
    version = (result.stdout or result.stderr).splitlines()
    return True, version[0].strip() if version else "available"


def _playwright_check() -> tuple[bool, str]:
    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            browser.close()
    except Exception as exc:
        return False, f"Chromium could not start ({type(exc).__name__})"
    return True, "headless Chromium started and closed"


def _deno_check() -> tuple[bool, str]:
    ok, detail = _tool_check("deno")
    match = re.match(r"deno (\d+)\.(\d+)\.(\d+)(?:\s|$)", detail) if ok else None
    if not match or tuple(map(int, match.groups())) < (2, 3, 0):
        return False, f"{detail}; Deno >=2.3.0 required; run scripts/install-deno.sh"
    return True, detail


def _ejs_check() -> tuple[bool, str]:
    try:
        import yt_dlp_ejs
        from yt_dlp_ejs.yt import solver

        expected = next(
            (item.split(";", 1)[0].removeprefix("yt-dlp-ejs==")
             for item in package_requires("yt-dlp") or () if item.startswith("yt-dlp-ejs==")),
            None,
        )
        if expected is None or yt_dlp_ejs.version != expected:
            return False, (
                f"EJS {yt_dlp_ejs.version}; yt-dlp requires {expected or 'an unknown version'}; "
                "sync the locked project dependencies"
            )
        if not solver.core().strip() or not solver.lib().strip():
            return False, "EJS solver bundles are empty; sync the locked project dependencies"
    except Exception as exc:
        return False, (
            f"EJS package/bundles unavailable ({type(exc).__name__}); "
            "sync the locked project dependencies"
        )
    return True, f"EJS {yt_dlp_ejs.version}; matching local core/lib bundles are available"


def _asr_model_ready(model: str, cache_root: Path) -> bool:
    try:
        resolve_cached_faster_whisper_model(model, cache_root, project_root=PROJECT_ROOT)
    except Exception:
        return False
    return True


def _model_check() -> tuple[bool | None, str]:
    provider = (os.getenv("PI_PROVIDER") or "deepseek").strip()
    if provider != "local":
        return None, f"skipped because PI_PROVIDER={provider}"
    try:
        models = discover_models()
    except LocalModelError as exc:
        return False, str(exc)
    explicit = (os.getenv("PI_MODEL") or "").strip()
    if not models:
        return False, "/models returned no model IDs"
    if explicit:
        return True, f"/models reachable ({len(models)} model ID(s)); explicit model is configured"
    if len(models) == 1:
        return True, f"/models reachable; one model can be selected automatically ({models[0]})"
    return False, f"/models returned {len(models)} IDs; set PI_MODEL to choose one"


def _asr_check() -> tuple[bool | None, str]:
    backend = (os.getenv("ASR_BACKEND") or "mlx").strip()
    if backend != "faster-whisper":
        return None, f"skipped because ASR_BACKEND={backend}"
    model = (os.getenv("ASR_MODEL") or DEFAULT_FASTER_WHISPER_MODEL).strip()
    cache_root = resolve_faster_whisper_cache_root(project_root=PROJECT_ROOT)
    if _asr_model_ready(model, cache_root):
        return True, f"weights are present for {model}"
    return False, f"weights are missing for {model}; run scripts/setup-wsl.sh"


def _asr_decoder_check() -> tuple[bool | None, str]:
    """Exercise faster-whisper's real media decoder without loading a model."""
    backend = (os.getenv("ASR_BACKEND") or "mlx").strip()
    if backend != "faster-whisper":
        return None, f"skipped because ASR_BACKEND={backend}"
    try:
        import av
        from faster_whisper.audio import decode_audio
    except Exception as exc:
        return False, f"faster-whisper audio decoder unavailable ({type(exc).__name__})"

    with tempfile.TemporaryDirectory(prefix="video-report-asr-doctor-") as temporary:
        audio_path = Path(temporary) / "decoder-probe.wav"
        with wave.open(str(audio_path), "wb") as audio_file:
            audio_file.setnchannels(1)
            audio_file.setsampwidth(2)
            audio_file.setframerate(16_000)
            audio_file.writeframes(bytes(16_000 // 4 * 2))
        try:
            decoded = decode_audio(str(audio_path), sampling_rate=16_000)
        except Exception as exc:
            return False, (
                f"synthetic WAV decode failed with PyAV {av.__version__} "
                f"({type(exc).__name__})"
            )
    if len(decoded) == 0:
        return False, f"synthetic WAV decoded to no samples with PyAV {av.__version__}"
    return True, f"synthetic WAV decoded with PyAV {av.__version__}; no model was loaded"


def run_doctor() -> list[tuple[str, bool | None, str]]:
    """Return status rows without sending chat/completion requests."""
    rows: list[tuple[str, bool | None, str]] = []
    python_ok = sys.version_info[:2] == (3, 12)
    rows.append(("Python", python_ok, f"{sys.version_info.major}.{sys.version_info.minor}."
                 f"{sys.version_info.micro}"))
    for name in ("uv", "node", "npm", "pi", "yt-dlp"):
        ok, detail = _tool_check(name)
        rows.append((name, ok, detail))
    rows.append(("YouTube Deno runtime", *_deno_check()))
    rows.append(("YouTube EJS scripts", *_ejs_check()))
    for name in ("ffmpeg", "ffprobe"):
        ok, detail = _tool_check(name, ("-version",))
        rows.append((name, ok, detail))
    rows.append(("Playwright Chromium", *_playwright_check()))
    rows.append(("Local model discovery", *_model_check()))
    rows.append(("faster-whisper audio decoder", *_asr_decoder_check()))
    rows.append(("faster-whisper weights", *_asr_check()))
    backend = (os.getenv("ASR_BACKEND") or "mlx").strip()
    if backend == "whisper-cpp":
        from .asr import AsrError
        from .whisper_cpp import DEFAULT_MODEL, resolve_whisper_cpp_config

        try:
            config = resolve_whisper_cpp_config(os.getenv("ASR_MODEL") or DEFAULT_MODEL)
            rows.append(("whisper.cpp files", True, config["executable"]))
        except (AsrError, ValueError, OSError) as exc:
            rows.append(("whisper.cpp files", False, str(exc)))
    return rows
