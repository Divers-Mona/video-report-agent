"""Per-run media limits and bounded checks inside the selected runs directory."""

from __future__ import annotations

import json
import math
import os
import stat
from pathlib import Path

DEFAULT_LIMITS = {
    "MEDIA_MAX_DURATION_SECONDS": 5 * 60 * 60,
    "MEDIA_MAX_FILE_BYTES": 1024**3,
    "MEDIA_MAX_DISK_BYTES": 10 * 1024**3,
}


class MediaLimitError(RuntimeError):
    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)


def resolve_media_limits(values: dict | None = None) -> dict[str, int]:
    limits = {}
    for name, default in DEFAULT_LIMITS.items():
        value = values.get(name, default) if values is not None else os.getenv(name, str(default))
        try:
            parsed = int(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{name} must be a positive integer") from exc
        if isinstance(value, bool) or not isinstance(value, (int, str)) or parsed <= 0:
            raise ValueError(f"{name} must be a positive integer")
        limits[name] = parsed
    return limits


def limits_for_run(run: Path) -> dict[str, int]:
    path = run / "input.json"
    if path.is_file():
        saved = json.loads(path.read_text(encoding="utf-8")).get("media_limits")
        if saved is not None:
            return resolve_media_limits(saved)
    return resolve_media_limits()


def duration_limit_text(limits: dict) -> str:
    seconds = limits["MEDIA_MAX_DURATION_SECONDS"]
    return f"{seconds} 秒" + (f"（{seconds // 3600} 小时）" if seconds % 3600 == 0 else "")


def check_duration(duration: object, limits: dict) -> None:
    if (
        isinstance(duration, bool)
        or not isinstance(duration, (int, float))
        or not math.isfinite(duration)
        or duration <= 0
    ):
        raise MediaLimitError("VIDEO_DURATION_INVALID", "无法确认视频时长，不能处理此视频。")
    maximum = limits["MEDIA_MAX_DURATION_SECONDS"]
    if duration > maximum:
        raise MediaLimitError(
            "VIDEO_TOO_LONG", f"视频时长超过配置上限 {duration_limit_text(limits)}。"
        )


def check_file_size(size: int, limits: dict) -> None:
    maximum = limits["MEDIA_MAX_FILE_BYTES"]
    if size > maximum:
        raise MediaLimitError("MEDIA_FILE_TOO_LARGE", f"下载文件超过配置上限 {maximum} 字节。")


def runs_size(root: Path) -> int:
    """Count logical regular-file bytes once per inode without following links."""
    root = root.resolve()
    seen = set()
    total = 0
    for directory, directories, files in os.walk(root, followlinks=False):
        directories[:] = [
            name
            for name in directories
            if not (Path(directory) / name).is_symlink()
            and (Path(directory) / name).resolve().is_relative_to(root)
        ]
        for name in files:
            path = Path(directory) / name
            try:
                info = path.lstat()
            except FileNotFoundError:
                continue
            identity = info.st_dev, info.st_ino
            if stat.S_ISREG(info.st_mode) and identity not in seen:
                seen.add(identity)
                total += info.st_size
    return total


def check_disk(run: Path, limits: dict, *, additional_bytes: int = 0) -> int:
    remaining = limits["MEDIA_MAX_DISK_BYTES"] - runs_size(run.parent)
    if remaining <= 0 or additional_bytes > remaining:
        raise MediaLimitError(
            "MEDIA_DISK_LIMIT",
            f"运行目录的磁盘用量将超过配置上限 {limits['MEDIA_MAX_DISK_BYTES']} 字节。"
            "请释放空间或提高 MEDIA_MAX_DISK_BYTES 后重试。",
        )
    return remaining


def check_download(run: Path, limits: dict, *, downloaded_bytes: int = 0) -> None:
    check_file_size(downloaded_bytes, limits)
    for path in (run / "download").glob("source.*"):
        if path.is_symlink():
            raise MediaLimitError("URL_INVALID", "下载文件不能是符号链接。")
        if path.is_file() and path.suffix not in {".json", ".srt", ".vtt", ".ass", ".ssa"}:
            check_file_size(path.stat().st_size, limits)
    check_disk(run, limits)
