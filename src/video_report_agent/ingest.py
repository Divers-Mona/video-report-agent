"""Bounded public Bilibili URL download and existing-ingest composition."""

from __future__ import annotations

import json
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qsl, urlsplit

import httpx

from .media_limits import (
    DEFAULT_LIMITS,
    MediaLimitError,
    check_disk,
    check_download,
    check_duration,
    duration_limit_text,
    limits_for_run,
    resolve_media_limits,
)

BILIBILI_HOSTS = frozenset({"bilibili.com", "www.bilibili.com"})
YOUTUBE_HOSTS = frozenset(
    {"youtube.com", "www.youtube.com", "m.youtube.com", "youtu.be"}
)
_BVID_PATTERN = re.compile(r"BV[0-9A-Za-z]{10}")
_YT_ID_PATTERN = re.compile(r"[A-Za-z0-9_-]{11}")
_PAGE_PATTERN = re.compile(r"[0-9]+")
_SOURCE_NAME = "source"
MAX_VIDEO_SECONDS = DEFAULT_LIMITS["MEDIA_MAX_DURATION_SECONDS"]
DOWNLOAD_CHECK_SECONDS = 30
DOWNLOAD_STALL_SECONDS = 10
DOWNLOAD_BUDGET_SECONDS = 120
RESOURCE_CHECK_SECONDS = 1
_PROGRESS_PREFIX = "vreport-progress:"

CommandRunner = Callable[..., Any]


def download_audio_only(metadata: dict) -> bool:
    """Use one download type for execution, cache identity and cancellation cleanup."""
    return bool(metadata.get("transcript_sources")) or metadata.get(
        "transcript_mode", "asr-only"
    ) in {"auto", "asr-only"}


@dataclass(frozen=True)
class VideoSource:
    """Common identity shared by every supported video platform."""

    platform: str
    submitted_url: str
    canonical_url: str
    platform_label: str


@dataclass(frozen=True)
class BilibiliSource(VideoSource):
    """The validated public source URL and its stable BVID identity."""

    bvid: str
    page_number: int

    @property
    def video_id(self) -> str:
        return f"bilibili-{self.bvid}-p{self.page_number}"

    @property
    def cache_key(self) -> str:
        return f"{self.bvid}/P{self.page_number}"


@dataclass(frozen=True)
class YoutubeSource(VideoSource):
    """The validated public YouTube URL and its stable 11-character video ID."""

    video_id_raw: str

    @property
    def video_id(self) -> str:
        return f"youtube-{self.video_id_raw}"

    @property
    def cache_key(self) -> str:
        return self.video_id_raw


@dataclass(frozen=True)
class DownloadResult:
    """Verified yt-dlp output and public metadata."""

    source: VideoSource
    media_path: Path
    info_path: Path
    title: str
    uploader: str
    attribution: str
    command: tuple[str, ...]


class UrlIngestError(RuntimeError):
    def __init__(self, category: str, message: str):
        self.category = category
        super().__init__(message)


def _parse_bilibili_url(value: object) -> tuple[str, str, int, str]:
    if not isinstance(value, str):
        raise UrlIngestError("URL_INVALID", "url must be a string")
    submitted_url = value.strip()
    # Bilibili's copied share text prefixes the link with a bracketed title.
    link = re.sub(r"^【.*】\s*", "", submitted_url, count=1, flags=re.DOTALL).strip()
    markdown_link = re.fullmatch(r"\[[^\]\r\n]*\]\(((?:https://|www\.)[^\s()]+)\)", link)
    if markdown_link:
        link = markdown_link.group(1)
    if link.startswith("www."):
        link = "https://" + link
    if link.startswith("https://b23.tv/"):
        short = urlsplit(link)
        if not re.fullmatch(r"/[A-Za-z0-9]+/?", short.path) or short.fragment:
            raise UrlIngestError("URL_INVALID", "无效的 Bilibili 短链接")
        try:
            response = httpx.get(link, follow_redirects=False, timeout=10)
        except httpx.HTTPError as exc:
            raise UrlIngestError(
                "URL_RESOLVE_FAILED", "Bilibili 短链接解析失败，请稍后重试或粘贴完整视频链接"
            ) from exc
        if response.status_code not in {301, 302, 303, 307, 308}:
            raise UrlIngestError("URL_RESOLVE_FAILED", "Bilibili 短链接未返回视频地址")
        # Validate the redirect below before any request to its destination.
        link = response.headers.get("location", "")
    normalized_url = (
        f"https://www.bilibili.com/video/{link}/"
        if _BVID_PATTERN.fullmatch(link)
        else link
    )
    try:
        parsed = urlsplit(normalized_url)
        hostname = parsed.hostname.lower() if parsed.hostname else None
        port = parsed.port
    except ValueError as exc:
        raise UrlIngestError("URL_INVALID", "url is not a valid HTTPS Bilibili URL") from exc
    if parsed.scheme != "https" or hostname not in BILIBILI_HOSTS:
        raise UrlIngestError("URL_INVALID", "url must use HTTPS and a Bilibili host")
    if parsed.username or parsed.password or port is not None:
        raise UrlIngestError("URL_INVALID", "url must not include credentials or a port")
    if parsed.fragment:
        raise UrlIngestError("URL_INVALID", "url must not include a fragment")
    match = re.fullmatch(r"/video/(BV[0-9A-Za-z]{10})/?", parsed.path)
    if match is None or _BVID_PATTERN.fullmatch(match.group(1)) is None:
        raise UrlIngestError("URL_INVALID", "url must use /video/<BVID>")
    bvid = match.group(1)
    page_values = [
        query_value
        for key, query_value in parse_qsl(parsed.query, keep_blank_values=True)
        if key == "p"
    ]
    if len(page_values) > 1 or (page_values and _PAGE_PATTERN.fullmatch(page_values[0]) is None):
        raise UrlIngestError("URL_INVALID", "p must be one positive page number")
    page_number = int(page_values[0]) if page_values else 1
    if page_number < 1:
        raise UrlIngestError("URL_INVALID", "p must be one positive page number")
    canonical_url = f"https://www.bilibili.com/video/{bvid}/"
    if page_values:
        canonical_url += f"?p={page_number}"
    return submitted_url, bvid, page_number, canonical_url


def clean_bilibili_url(value: object) -> str:
    """Keep the BVID and an optional numeric page, dropping other URL data."""

    return _parse_bilibili_url(value)[3]


def validate_bilibili_url(value: object) -> BilibiliSource:
    """Validate a public BV URL, resolving b23.tv share links when necessary."""

    submitted_url, bvid, page_number, canonical_url = _parse_bilibili_url(value)
    return BilibiliSource(
        platform="bilibili",
        submitted_url=submitted_url,
        canonical_url=canonical_url,
        platform_label="Bilibili",
        bvid=bvid,
        page_number=page_number,
    )


def _parse_youtube_url(value: object) -> tuple[str, str]:
    """Return (submitted_url, video_id) for one public YouTube video link."""

    if not isinstance(value, str):
        raise UrlIngestError("URL_INVALID", "url must be a string")
    submitted_url = value.strip()
    # Share text may prefix the link; reuse the same bracket/markdown stripping.
    link = re.sub(r"^【.*】\s*", "", submitted_url, count=1, flags=re.DOTALL).strip()
    markdown_link = re.fullmatch(
        r"\[[^\]\n]*\]\(((?:https?://|www\.)[^\s)]+)\)", link
    )
    if markdown_link:
        link = markdown_link.group(1)
    if link.startswith("www."):
        link = "https://" + link
    # A bare 11-character ID is normalized to its canonical watch URL.
    if _YT_ID_PATTERN.fullmatch(link):
        return submitted_url, link
    parts = urlsplit(link)
    if parts.scheme not in {"http", "https"} or parts.netloc.lower() not in YOUTUBE_HOSTS:
        raise UrlIngestError("URL_INVALID", "url must be a YouTube video link")
    host = parts.netloc.lower().removeprefix("www.")
    query = dict(parse_qsl(parts.query))
    if host == "youtu.be":
        path_id = parts.path.lstrip("/").split("/")
        if len(path_id) != 1 or not _YT_ID_PATTERN.fullmatch(path_id[0]):
            raise UrlIngestError("URL_INVALID", "url must be a YouTube video link")
        return submitted_url, path_id[0]
    segments = [seg for seg in parts.path.split("/") if seg]
    if segments and segments[0] == "watch":
        video_id = query.get("v")
        if not (video_id and _YT_ID_PATTERN.fullmatch(video_id)):
            raise UrlIngestError("URL_INVALID", "url must be a YouTube video link")
        return submitted_url, video_id
    if segments and segments[0] == "shorts" and len(segments) == 2:
        if not _YT_ID_PATTERN.fullmatch(segments[1]):
            raise UrlIngestError("URL_INVALID", "url must be a YouTube video link")
        return submitted_url, segments[1]
    raise UrlIngestError("URL_INVALID", "url must be a YouTube video link")


def validate_youtube_url(value: object) -> YoutubeSource:
    """Validate a public YouTube watch/shorts/youtu.be link to one video."""

    submitted_url, video_id = _parse_youtube_url(value)
    canonical_url = f"https://www.youtube.com/watch?v={video_id}"
    return YoutubeSource(
        platform="youtube",
        submitted_url=submitted_url,
        canonical_url=canonical_url,
        platform_label="YouTube",
        video_id_raw=video_id,
    )


def resolve_source(value: object) -> VideoSource:
    """Dispatch a submitted link to its platform parser before any run is created."""

    if not isinstance(value, str):
        raise UrlIngestError("URL_INVALID", "url must be a string")
    text = value.strip()
    link = re.sub(r"^【.*】\s*", "", text, count=1, flags=re.DOTALL).strip()
    markdown_link = re.fullmatch(
        r"\[[^\]\n]*\]\(((?:https?://|www\.)[^\s)]+)\)", link
    )
    if markdown_link:
        link = markdown_link.group(1)
    if _BVID_PATTERN.fullmatch(link):
        return validate_bilibili_url(value)
    if _YT_ID_PATTERN.fullmatch(link):
        return validate_youtube_url(value)
    try:
        host = urlsplit("https://" + link if link.startswith("www.") else link).hostname
    except ValueError as exc:
        raise UrlIngestError("URL_INVALID", "url must be a valid video link") from exc
    if host in BILIBILI_HOSTS or host == "b23.tv":
        return validate_bilibili_url(value)
    if host in YOUTUBE_HOSTS:
        return validate_youtube_url(value)
    raise UrlIngestError("URL_INVALID", "url must be a Bilibili or YouTube video link")


def probe_bilibili_video(
    source: BilibiliSource, *, runner: CommandRunner | None = None,
) -> dict:
    """Read the selected part's metadata without downloading media or writing files."""
    executable = shutil.which("yt-dlp")
    if executable is None:
        raise UrlIngestError("VIDEO_METADATA_UNAVAILABLE", "视频时长查询暂不可用。")
    command = [
        sys.executable, "-m", "video_report_agent.download_worker", "0",
        "--ignore-config", "--no-playlist", "--skip-download",
        "--dump-single-json", "--socket-timeout", "15", "--retries", "0",
        source.canonical_url,
    ]
    try:
        result = (runner or subprocess.run)(
            command, capture_output=True, text=True, check=False, shell=False, timeout=45,
        )
        if result.returncode != 0:
            raise ValueError("metadata command failed")
        payload = json.loads(result.stdout)
        if not isinstance(payload, dict) or payload.get("_type") in {"playlist", "multi_video"}:
            raise ValueError("expected a single video part")
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        raise UrlIngestError(
            "VIDEO_METADATA_UNAVAILABLE", "无法查询视频时长，请稍后重试。",
        ) from exc
    duration = payload.get("duration")
    try:
        check_duration(duration, resolve_media_limits())
    except MediaLimitError as exc:
        raise UrlIngestError(exc.code, str(exc)) from exc
    return {
        "bvid": source.bvid, "page_number": source.page_number,
        "video_id": source.video_id, "url": source.canonical_url,
        "title": str(payload.get("title") or source.bvid), "duration": duration,
    }


def _safe_download_line(line: str) -> str:
    return re.sub(r"https?://[^\s\"'<>]+", "[media-url]", line)


def _download_needs_switch(progress: dict, now: float, last_change: float) -> bool:
    if progress.get("status") == "finished":
        return False
    if now - last_change >= DOWNLOAD_STALL_SECONDS:
        return True
    downloaded = progress.get("downloaded_bytes") or 0
    total = progress.get("total_bytes") or progress.get("total_bytes_estimate") or 0
    downloaded = downloaded if isinstance(downloaded, (int, float)) else 0
    total = total if isinstance(total, (int, float)) else 0
    eta = progress.get("eta")
    return not ((total and downloaded / total >= 0.9) or (
        isinstance(eta, (int, float)) and 0 <= eta <= DOWNLOAD_CHECK_SECONDS
    ))


def _check_download_limits(run_dir: Path, limits: dict, downloaded_bytes=0) -> None:
    try:
        check_download(run_dir, limits, downloaded_bytes=downloaded_bytes)
    except MediaLimitError as exc:
        raise UrlIngestError(exc.code, str(exc)) from exc


def _run_download(command: list[str], run_dir: Path, runner: CommandRunner | None) -> Any:
    limits = limits_for_run(run_dir)
    # The injected runner is retained for callers testing output validation.
    if runner is not None:
        try:
            result = runner(command, capture_output=True, text=True, check=False,
                            shell=False, timeout=DOWNLOAD_BUDGET_SECONDS)
        except (OSError, subprocess.SubprocessError) as exc:
            raise UrlIngestError(
                "DOWNLOAD_ERROR", "视频下载失败，请稍后重试或联系管理员。"
            ) from exc
        (run_dir / "download.log").write_text(_safe_download_line(str(result.stderr)))
        _check_download_limits(run_dir, limits)
        return result

    deadline = time.monotonic() + DOWNLOAD_BUDGET_SECONDS
    with (run_dir / "download.log").open("w", encoding="utf-8", buffering=1) as log:
        for attempt in range(2):
            if time.monotonic() >= deadline:
                break
            mode = "backup-or-reparse" if attempt else "primary"
            log.write(f"[download] attempt={attempt + 1} mode={mode}\n")
            worker = [sys.executable, "-m", "video_report_agent.download_worker",
                      str(attempt), *command[1:]]
            lines: queue.Queue = queue.Queue()
            stdout, stderr = [], []
            progress = {}
            last_bytes = 0
            started = last_change = time.monotonic()
            next_check = started + DOWNLOAD_CHECK_SECONDS
            next_resource_check = started
            reason = None
            try:
                process = subprocess.Popen(worker, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                           text=True, bufsize=1)
            except OSError as exc:
                raise UrlIngestError(
                    "DOWNLOAD_ERROR", "视频下载暂不可用，请稍后重试或联系管理员。"
                ) from exc

            def read_lines(pipe, stream):
                for line in pipe:
                    lines.put((stream, line))
                lines.put((stream, None))

            threads = [threading.Thread(target=read_lines, args=(pipe, stream), daemon=True)
                       for pipe, stream in ((process.stdout, "stdout"), (process.stderr, "stderr"))]
            for thread in threads:
                thread.start()
            closed = 0
            try:
                while closed < 2 or process.poll() is None:
                    now = time.monotonic()
                    if now >= deadline:
                        reason = "total-budget-exhausted"
                        break
                    if now >= next_resource_check:
                        size = progress.get("downloaded_bytes")
                        _check_download_limits(
                            run_dir, limits, size if isinstance(size, (int, float)) else 0
                        )
                        next_resource_check = now + RESOURCE_CHECK_SECONDS
                    if now >= next_check:
                        switch = _download_needs_switch(progress, now, last_change)
                        action = "switch" if switch and attempt == 0 else "continue"
                        log.write(
                            f"[download] checkpoint attempt={attempt + 1} action={action}\n"
                        )
                        if switch and attempt == 0:
                            reason = "slow-or-stalled"
                            break
                        next_check = now + DOWNLOAD_CHECK_SECONDS
                    try:
                        stream, line = lines.get(timeout=min(0.1, max(0.001, deadline - now)))
                    except queue.Empty:
                        continue
                    if line is None:
                        closed += 1
                        continue
                    if line.startswith(_PROGRESS_PREFIX):
                        try:
                            progress = json.loads(line[len(_PROGRESS_PREFIX):])
                        except ValueError:
                            continue
                        downloaded = progress.get("downloaded_bytes") or 0
                        if downloaded != last_bytes or progress.get("status") == "finished":
                            last_bytes, last_change = downloaded, now
                        # This template contains only status, bytes, ETA and speed.
                        log.write(line)
                    else:
                        safe = _safe_download_line(line)
                        (stdout if stream == "stdout" else stderr).append(safe)
                        if stream == "stderr":
                            log.write(safe)
            finally:
                if process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait()
                for thread in threads:
                    thread.join(timeout=1)
                process.stdout.close()
                process.stderr.close()
            completed = subprocess.CompletedProcess(worker, process.returncode,
                                                    "".join(stdout), "".join(stderr))
            log.write(
                f"[download] attempt={attempt + 1} returncode={process.returncode} "
                f"reason={reason or 'exited'}\n"
            )
            if reason == "total-budget-exhausted":
                break
            if reason is None and completed.returncode in (0, 101):
                _check_download_limits(run_dir, limits)
                return completed
            if attempt == 0:
                log.write("[download] retry=1 use-platform-backup-or-reparse\n")
            else:
                return completed
        log.write("[download] stopped reason=total-budget-exhausted\n")
    raise UrlIngestError("DOWNLOAD_ERROR", "视频下载超时，请稍后重试；如果反复失败，请联系管理员。")


def _inside(path: Path, root: Path, *, category: str, message: str) -> Path:
    resolved_root = root.resolve()
    resolved_path = path.resolve()
    try:
        resolved_path.relative_to(resolved_root)
    except ValueError as exc:
        raise UrlIngestError(category, message) from exc
    return resolved_path


def _printed_media_path(stdout: object, download_dir: Path) -> Path:
    if not isinstance(stdout, str):
        raise UrlIngestError("DOWNLOAD_ERROR", "yt-dlp did not report a media path")
    for line in reversed(stdout.splitlines()):
        candidate_text = line.strip()
        if not candidate_text:
            continue
        candidate = Path(candidate_text)
        if not candidate.is_absolute():
            candidate = download_dir / candidate
        if candidate.name.startswith(f"{_SOURCE_NAME}.") and candidate.name != "source.info.json":
            return candidate
    raise UrlIngestError("DOWNLOAD_ERROR", "yt-dlp did not report a media path")


def _metadata(info_path: Path, source: VideoSource) -> tuple[str, str, str]:
    try:
        payload = json.loads(info_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise UrlIngestError("DOWNLOAD_ERROR", "yt-dlp metadata is unavailable") from exc
    if not isinstance(payload, dict):
        raise UrlIngestError("DOWNLOAD_ERROR", "yt-dlp metadata is invalid")
    if payload.get("is_live") or payload.get("live_status") in {"is_live", "is_upcoming"}:
        raise UrlIngestError("URL_INVALID", "暂不支持正在直播或即将直播的视频。")
    duration = payload.get("duration")
    try:
        check_duration(duration, limits_for_run(info_path.parent.parent))
    except MediaLimitError as exc:
        raise UrlIngestError(exc.code, str(exc)) from exc
    title = payload.get("title")
    uploader = payload.get("uploader") or payload.get("channel")
    if not isinstance(title, str) or not title.strip():
        raise UrlIngestError("DOWNLOAD_ERROR", "yt-dlp metadata has no title")
    if not isinstance(uploader, str) or not uploader.strip():
        raise UrlIngestError("DOWNLOAD_ERROR", "yt-dlp metadata has no uploader")
    title = title.strip()
    uploader = uploader.strip()
    attribution = f"{source.platform_label}；{uploader}；《{title}》；{source.canonical_url}"
    return title, uploader, attribution


def _download_failure(source: VideoSource, error: str) -> UrlIngestError:
    text = error.lower()
    platform = source.platform_label
    if "http error 412" in text:
        return UrlIngestError(
            "DOWNLOAD_PLATFORM_BLOCKED",
            f"视频链接已识别，但 {platform} 拒绝了下载请求（HTTP 412）。"
            "可能需要完成平台风控或人机验证，请稍后重试。",
        )
    if any(token in text for token in (
        "not a bot", "captcha", "geetest", "riskhit", "risk-captcha-sdk",
        "exceeded the rate limit", "人机验证", "风控",
    )):
        return UrlIngestError("DOWNLOAD_PLATFORM_BLOCKED", f"{platform} 要求风控或人机验证。")
    if source.platform == "bilibili" and "unable to extract initial state" in text:
        return UrlIngestError(
            "DOWNLOAD_PLATFORM_BLOCKED",
            "Bilibili 页面解析失败（Unable to extract initial state），"
            "可能遇到平台风控或人机验证。",
        )
    if any(token in text for token in (
        "members-only", "members only", "requires payment", "paid video", "premium member",
        "大会员", "付费", "充电专属",
    )):
        return UrlIngestError(
            "DOWNLOAD_MEMBERSHIP_REQUIRED", f"{platform} 视频需要付费或会员权限。"
        )
    if any(token in text for token in (
        "not available in your country", "not made this video available in your country",
        "geo-restricted", "geographic restriction", "地区限制",
    )):
        return UrlIngestError("DOWNLOAD_GEO_RESTRICTED", f"{platform} 视频受地区限制。")
    if any(token in text for token in (
        "sign in", "login required", "log in", "requires authentication",
        "only available for registered users", "需要登录",
    )):
        return UrlIngestError("DOWNLOAD_LOGIN_REQUIRED", f"{platform} 视频需要登录后访问。")
    return UrlIngestError("DOWNLOAD_ERROR", f"{platform} 视频下载失败，请稍后重试。")


def download_bilibili_video(
    source: VideoSource,
    run_dir: Path,
    *,
    runner: CommandRunner | None = None,
    request_subtitles: bool = False,
    audio_only: bool = False,
) -> DownloadResult:
    """Run the yt-dlp command and verify its run-local output."""

    download_dir = run_dir.resolve() / "download"
    if download_dir.is_symlink():
        raise UrlIngestError("URL_INVALID", "下载目录不能是符号链接。")
    _inside(download_dir, run_dir, category="URL_INVALID", message="下载目录超出任务范围。")
    try:
        download_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise UrlIngestError("DOWNLOAD_ERROR", "download directory is unavailable") from exc
    if any(path.is_symlink() for path in download_dir.iterdir()):
        raise UrlIngestError("URL_INVALID", "下载目录中不能包含符号链接。")
    limits = limits_for_run(run_dir)
    try:
        remaining = check_disk(run_dir, limits)
    except MediaLimitError as exc:
        raise UrlIngestError(exc.code, str(exc)) from exc
    executable = shutil.which("yt-dlp")
    if executable is None:
        raise UrlIngestError("DOWNLOAD_ERROR", "yt-dlp is unavailable")
    command = [
        executable,
        "--ignore-config",
        "--no-playlist",
        "--match-filter",
        f"duration <= {limits['MEDIA_MAX_DURATION_SECONDS']} & !is_live & live_status !=? is_live "
        "& live_status !=? is_upcoming",
        "--max-filesize", str(min(limits["MEDIA_MAX_FILE_BYTES"], remaining)),
        "--break-on-reject",
        "-P",
        str(download_dir),
        "-o",
        "source.%(ext)s",
        "--write-info-json",
        "--print",
        "after_move:filepath",
        "--progress", "--newline", "--progress-delta", "2",
        "--progress-template",
        'download:vreport-progress:{"status":%(progress.status)j,'
        '"downloaded_bytes":%(progress.downloaded_bytes|null)j,'
        '"total_bytes":%(progress.total_bytes|null)j,'
        '"total_bytes_estimate":%(progress.total_bytes_estimate|null)j,'
        '"eta":%(progress.eta|null)j,"speed":%(progress.speed|null)j}',
        "--socket-timeout", "10", "--retries", "0", "--fragment-retries", "0",
        "--extractor-retries", "0",
    ]
    if audio_only:
        command.extend(["-f", "bestaudio"])
    else:
        command.extend(["-S", "res:1080,vcodec:h264,acodec:aac", "--merge-output-format", "mp4"])
    if request_subtitles:
        command.extend(["--write-subs", "--sub-langs", "all", "--sub-format", "srt/vtt/ass/best"])
    command.append(source.canonical_url)
    completed = _run_download(command, run_dir, runner)
    error = str(getattr(completed, "stderr", ""))
    if "larger than max-filesize" in error.lower():
        disk_limited = remaining < limits["MEDIA_MAX_FILE_BYTES"]
        raise UrlIngestError(
            "MEDIA_DISK_LIMIT" if disk_limited else "MEDIA_FILE_TOO_LARGE",
            "视频下载将超过运行目录磁盘上限。" if disk_limited
            else f"下载文件超过配置上限 {limits['MEDIA_MAX_FILE_BYTES']} 字节。",
        )
    if getattr(completed, "returncode", None) == 101 or (
        not str(getattr(completed, "stdout", "")).strip()
        and "does not pass filter" in str(getattr(completed, "stderr", ""))
    ):
        raise UrlIngestError(
            "VIDEO_DURATION_INVALID",
            f"当前仅支持不超过 {duration_limit_text(limits)}的非直播视频，"
            "不支持正在直播或即将直播的视频。",
        )
    if getattr(completed, "returncode", 1) != 0:
        raise _download_failure(source, error)
    media_path = _inside(
        _printed_media_path(getattr(completed, "stdout", None), download_dir),
        download_dir,
        category="DOWNLOAD_ERROR",
        message="downloaded media path is outside the run directory",
    )
    if not media_path.is_file() or media_path.stat().st_size == 0:
        raise UrlIngestError("DOWNLOAD_ERROR", "downloaded media is missing")
    info_path = download_dir / "source.info.json"
    if not info_path.is_file():
        raise UrlIngestError("DOWNLOAD_ERROR", "yt-dlp metadata file is missing")
    title, uploader, attribution = _metadata(info_path, source)
    result = DownloadResult(
        source=source,
        media_path=media_path,
        info_path=info_path.resolve(),
        title=title,
        uploader=uploader,
        attribution=attribution,
        command=tuple(command),
    )
    return result
