"""Offline whisper.cpp CLI adapter with timestamp and GPU-use validation."""

from __future__ import annotations

import base64
import json
import os
import re
import subprocess
import tempfile
from pathlib import Path
from time import perf_counter
from uuid import uuid4

from .asr import AsrError, AsrModelMissingError, AsrRun, normalize_asr_segments

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_MODEL = "large-v3-turbo"


def restore_whisper_cpp_raw_output(run: Path, asr: dict) -> None:
    """Restore repaired-byte evidence from the durable ASR/cache document."""
    if asr.get("backend") != "whisper-cpp":
        return
    raw = asr.get("raw_result", {})
    if not raw.get("utf8_repairs"):
        return
    encoded = raw.get("raw_output_base64")
    if not isinstance(encoded, str):
        raise AsrError("whisper.cpp repaired text has no durable original byte evidence")
    try:
        original = base64.b64decode(encoded, validate=True)
    except ValueError as exc:
        raise AsrError("whisper.cpp original byte evidence is invalid base64") from exc
    (run / "whisper-cpp.raw.json").write_bytes(original)


def parse_whisper_cpp_json(data: bytes) -> tuple[dict, list[dict], list[dict]]:
    """Keep invalid UTF-8 visible only in segment text, with original byte ranges."""
    # turbo can emit a partial byte token as segment text (upstream #3760).
    # Unique ASCII placeholders let JSON parsing establish the damaged field;
    # errors in keys/metadata/syntax must not be silently accepted.
    marker_prefix = f"__whisper_cpp_utf8_{uuid4().hex}_"
    parts, damaged = [], []
    offset = 0
    while offset < len(data):
        try:
            parts.append(data[offset:].decode("utf-8"))
            break
        except UnicodeDecodeError as exc:
            start, end = offset + exc.start, offset + exc.end
            parts.append(data[offset:start].decode("utf-8"))
            marker = f"{marker_prefix}{len(damaged)}__"
            parts.append(marker)
            damaged.append((marker, {
                "raw_json_byte_start": start, "raw_json_byte_end": end,
                "invalid_bytes_hex": data[start:end].hex(), "reason": exc.reason,
            }))
            offset = end
    try:
        payload = json.loads("".join(parts))
    except ValueError as exc:
        raise AsrError("whisper.cpp returned invalid JSON syntax") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("transcription"), list):
        raise AsrError("whisper.cpp returned an invalid transcription payload")
    rows, repairs = [], []
    for ordinal, row in enumerate(payload["transcription"]):
        if not isinstance(row, dict) or not isinstance(row.get("offsets"), dict):
            raise AsrError(f"whisper.cpp segment {ordinal} has no timestamp offsets")
        offsets = row["offsets"]
        if any(type(offsets.get(key)) is not int for key in ("from", "to")):
            raise AsrError(f"whisper.cpp segment {ordinal} has invalid timestamp offsets")
        text = row.get("text")
        if not isinstance(text, str):
            raise AsrError(f"whisper.cpp segment {ordinal} has no text")
        for marker, damage in damaged:
            if marker in text:
                repairs.append({
                    **damage, "raw_segment_ordinal": ordinal,
                    "start_ms": offsets["from"], "end_ms": offsets["to"],
                    "text_character_offset": text.index(marker), "replacement": "\ufffd",
                })
                text = text.replace(marker, "\ufffd")
        row["text"] = text
        rows.append({
            "start": offsets["from"] / 1000, "end": offsets["to"] / 1000, "text": text,
        })
    serialized = json.dumps(payload, ensure_ascii=False)
    if marker_prefix in serialized or len(repairs) != len(damaged):
        raise AsrError("whisper.cpp returned invalid UTF-8 outside segment text")
    try:
        serialized.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise AsrError("whisper.cpp returned an invalid Unicode escape") from exc
    return payload, rows, repairs


def resolve_whisper_cpp_config(model: str, *, require_files=True) -> dict:
    """Snapshot local executable/weights identity for persisted cache compatibility."""
    if not isinstance(model, str) or not model.strip():
        raise ValueError("whisper-cpp requires a ggml model file or large-v3-turbo")
    model_path = (
        Path("models/whisper-cpp/ggml-large-v3-turbo.bin")
        if model == DEFAULT_MODEL else Path(model).expanduser()
    )
    executable = Path(os.getenv(
        "WHISPER_CPP_BINARY", ".tooling/wsl/whisper.cpp/build/bin/whisper-cli"
    )).expanduser()
    result = {"gpu": True, "flash_attn": True}
    for name, path in (("model_path", model_path), ("executable", executable)):
        path = (PROJECT_ROOT / path).resolve() if not path.is_absolute() else path.resolve()
        result[name] = str(path)
        if not path.is_file() or path.stat().st_size == 0:
            if not require_files:
                result[name + "_size"] = None
                result[name + "_mtime_ns"] = None
                continue
            raise AsrModelMissingError(
                f"whisper.cpp local {name} is missing or empty: {path}; "
                "run bash scripts/setup-whisper-cpp-hip.sh first"
            )
        if require_files and name == "executable" and not os.access(path, os.X_OK):
            raise AsrError(f"whisper.cpp binary is not executable: {path}")
        stat = path.stat()
        result[name + "_size"] = stat.st_size
        result[name + "_mtime_ns"] = stat.st_mtime_ns
    return result


class WhisperCppBackend:
    default_model = DEFAULT_MODEL

    def __init__(self, model=DEFAULT_MODEL, *, parameters=None, task_path=None):
        self.model = model
        self.parameters = (
            resolve_whisper_cpp_config(model) if parameters is None else parameters
        )
        self.task_path = task_path

    def transcribe(self, audio_path: Path, language="zh") -> AsrRun:
        if not audio_path.is_file() or audio_path.stat().st_size == 0:
            raise AsrError(f"audio input is missing or empty: {audio_path}")
        # Persisted run settings must still describe the files actually executed.
        for name in ("model_path", "executable"):
            path = Path(self.parameters[name])
            if not path.is_file():
                raise AsrModelMissingError(f"whisper.cpp {name} is missing: {path}")
            stat = path.stat()
            if (stat.st_size, stat.st_mtime_ns) != (
                self.parameters[name + "_size"], self.parameters[name + "_mtime_ns"]
            ):
                raise AsrError(f"whisper.cpp {name} changed after run creation: {path}")
        log_path = (
            Path(self.task_path).with_name("whisper-cpp.log")
            if self.task_path else audio_path.with_name("whisper-cpp.log")
        )
        started = perf_counter()
        with tempfile.TemporaryDirectory(prefix="whisper-cpp-", dir=audio_path.parent) as temporary:
            prefix = Path(temporary) / "result"
            command = [
                self.parameters["executable"], "--model", self.parameters["model_path"],
                "--file", str(audio_path.resolve()), "--language", language,
                "--output-json", "--output-file", str(prefix),
                "--flash-attn", "--device", "0",
            ]
            # Inherit the supervised pipeline's process group so cancellation/deadline
            # terminates inference together with its Python worker.
            with log_path.open("wb") as log:
                try:
                    completed = subprocess.run(command, stdout=log, stderr=log, check=False)
                except OSError as exc:
                    raise AsrError(f"Cannot start whisper.cpp: {exc}") from exc
            raw_path = log_path.with_name("whisper-cpp.raw.json")
            raw_bytes = None
            output_path = prefix.with_suffix(".json")
            if output_path.is_file():
                raw_bytes = output_path.read_bytes()
                # Preserve exact output before decoding, including malformed results.
                raw_path.write_bytes(raw_bytes)
            diagnostic = log_path.read_text(encoding="utf-8", errors="replace")
            if completed.returncode:
                raise AsrError(
                    f"whisper.cpp exited with status {completed.returncode}; see {log_path}"
                )
            gpu = re.search(r"whisper_backend_init_gpu: using (ROCm\d+) backend", diagnostic)
            gpu_weights = re.search(
                r"whisper_model_load:\s+ROCm\d+ total size =\s+([\d.]+) MB", diagnostic
            )
            # v1.9.5 prints "using" before dev_init; a later failure falls back
            # to CPU. Require actual GPU weight allocation and no init failure.
            if (
                not gpu or not gpu_weights or float(gpu_weights.group(1)) <= 0
                or "whisper_backend_init_gpu: failed to initialize" in diagnostic
            ):
                raise AsrError(f"whisper.cpp did not initialize a HIP GPU backend; see {log_path}")
            if raw_bytes is None:
                raise AsrError(f"whisper.cpp produced no JSON; see {log_path}")
            payload, rows, repairs = parse_whisper_cpp_json(raw_bytes)
        result = payload.get("result")
        detected = result.get("language") if isinstance(result, dict) else None
        if not isinstance(detected, str) or not detected:
            raise AsrError("whisper.cpp returned no detected language")
        normalized = normalize_asr_segments(rows)
        return AsrRun(
            engine="whisper.cpp", backend="whisper-cpp", model=self.model,
            language=detected, elapsed_ms=round((perf_counter() - started) * 1000),
            segments=normalized.segments,
            raw_result={
                **payload, "segments": rows, "gpu_backend": gpu.group(1),
                "utf8_repairs": repairs,
                **({
                    "raw_output_artifact": raw_path.name,
                    "raw_output_base64": base64.b64encode(raw_bytes).decode("ascii"),
                } if repairs else {}),
            },
            dropped_empty_raw_segment_ordinals=normalized.dropped_empty_raw_segment_ordinals,
            dropped_non_positive_raw_segment_ordinals=(
                normalized.dropped_non_positive_raw_segment_ordinals
            ),
            dropped_unrepresentable_raw_segment_ordinals=(
                normalized.dropped_unrepresentable_raw_segment_ordinals
            ),
        )
