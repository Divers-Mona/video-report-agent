import json
import os
import sys
import time
from types import SimpleNamespace

import pytest
from asr_fixtures import provide_faster_whisper_cache
from test_download_recovery import controlled_workers
from test_youtube_pipeline import local_stages as local_stages
from yt_dlp.utils import match_filter_func

from video_report_agent import asr, pipeline
from video_report_agent.ingest import (
    UrlIngestError,
    _run_download,
    download_bilibili_video,
    resolve_source,
)
from video_report_agent.media_config import resolve_media_config
from video_report_agent.retention import cleanup_media
from video_report_agent.reuse import cache_key
from video_report_agent.storage import Store

URL = "https://www.youtube.com/watch?v=jNQXAC9IVRw"
BILIBILI = "https://www.bilibili.com/video/BV1qxn16yEvP/"


def result(stdout="", stderr="", code=0):
    return SimpleNamespace(stdout=stdout, stderr=stderr, returncode=code)


def successful_download(run, command, *, size=16, duration=3):
    media = run / "download/source.m4a"
    media.write_bytes(b"x" * size)
    (media.parent / "source.info.json").write_text(
        json.dumps(
            {
                "title": "Fixture",
                "uploader": "Author",
                "duration": duration,
            }
        )
    )
    return result(str(media))


def test_duration_limit_is_applied_before_downloading(tmp_path, monkeypatch):
    monkeypatch.setenv("MEDIA_MAX_DURATION_SECONDS", "3")
    monkeypatch.setattr("video_report_agent.ingest.shutil.which", lambda _: "/test/yt-dlp")

    def runner(command, **kwargs):
        rule = command[command.index("--match-filter") + 1]
        assert match_filter_func(rule)({"title": "Fixture", "duration": 4}, incomplete=False)
        return result(stderr="does not pass filter", code=101)

    with pytest.raises(UrlIngestError, match="3"):
        download_bilibili_video(resolve_source(URL), tmp_path, runner=runner)
    assert not list((tmp_path / "download").iterdir())


def test_file_limit_is_passed_to_downloader(tmp_path, monkeypatch):
    monkeypatch.setenv("MEDIA_MAX_FILE_BYTES", "10")
    monkeypatch.setattr("video_report_agent.ingest.shutil.which", lambda _: "/test/yt-dlp")

    def runner(command, **kwargs):
        assert command[command.index("--max-filesize") + 1] == "10"
        return result(stderr="File is larger than max-filesize (12 bytes > 10 bytes). Aborting.")

    with pytest.raises(UrlIngestError) as caught:
        download_bilibili_video(resolve_source(URL), tmp_path, runner=runner)
    assert caught.value.category == "MEDIA_FILE_TOO_LARGE"


def test_unknown_file_size_is_checked_after_download(tmp_path, monkeypatch):
    monkeypatch.setenv("MEDIA_MAX_FILE_BYTES", "10")
    monkeypatch.setattr("video_report_agent.ingest.shutil.which", lambda _: "/test/yt-dlp")
    with pytest.raises(UrlIngestError) as caught:
        download_bilibili_video(
            resolve_source(URL),
            tmp_path,
            runner=lambda command, **kw: successful_download(tmp_path, command, size=12),
        )
    assert caught.value.category == "MEDIA_FILE_TOO_LARGE"


def test_full_runs_directory_rejects_download_before_start(tmp_path, monkeypatch):
    runs = tmp_path / "runs"
    run = runs / "current"
    run.mkdir(parents=True)
    (runs / "existing.bin").write_bytes(b"x" * 101)
    monkeypatch.setenv("MEDIA_MAX_DISK_BYTES", "100")
    monkeypatch.setattr("video_report_agent.ingest.shutil.which", lambda _: "/test/yt-dlp")
    with pytest.raises(UrlIngestError) as caught:
        download_bilibili_video(
            resolve_source(URL),
            run,
            runner=lambda *a, **kw: pytest.fail("disk quota must be checked before download"),
        )
    assert caught.value.category == "MEDIA_DISK_LIMIT"
    assert (runs / "existing.bin").read_bytes() == b"x" * 101


def test_disk_limit_does_not_follow_external_links(tmp_path, monkeypatch):
    external = tmp_path / "external"
    external.mkdir()
    (external / "canary").write_bytes(b"x" * 5000)
    run = tmp_path / "runs/current"
    run.mkdir(parents=True)
    (run.parent / "linked").symlink_to(external, target_is_directory=True)
    monkeypatch.setenv("MEDIA_MAX_DISK_BYTES", "1024")
    monkeypatch.setattr("video_report_agent.ingest.shutil.which", lambda _: "/test/yt-dlp")
    download_bilibili_video(
        resolve_source(URL),
        run,
        runner=lambda command, **kw: successful_download(run, command),
    )
    assert (external / "canary").read_bytes() == b"x" * 5000


def test_download_directory_cannot_redirect_writes_outside_run(tmp_path, monkeypatch):
    external = tmp_path / "external"
    external.mkdir()
    run = tmp_path / "run"
    run.mkdir()
    (run / "download").symlink_to(external, target_is_directory=True)
    monkeypatch.setattr("video_report_agent.ingest.shutil.which", lambda _: "/test/yt-dlp")
    with pytest.raises(UrlIngestError):
        download_bilibili_video(
            resolve_source(URL),
            run,
            runner=lambda *a, **kw: pytest.fail("unsafe download directory reached the runner"),
        )
    assert not list(external.iterdir())


def test_printed_download_path_cannot_escape_run(tmp_path, monkeypatch):
    external = tmp_path / "external/source.m4a"
    external.parent.mkdir()
    external.write_bytes(b"canary")
    run = tmp_path / "run"
    run.mkdir()
    monkeypatch.setattr("video_report_agent.ingest.shutil.which", lambda _: "/test/yt-dlp")
    with pytest.raises(UrlIngestError, match="outside"):
        download_bilibili_video(
            resolve_source(URL),
            run,
            runner=lambda *a, **kw: result(str(external)),
        )
    assert external.read_bytes() == b"canary"


def test_imported_file_is_read_only_and_export_name_cannot_traverse(tmp_path):
    source = tmp_path / "external/source.ja.srt"
    source.parent.mkdir()
    source.write_text("Original subtitle")
    root = tmp_path / "runs"
    file_run = pipeline.create_run(
        root,
        BILIBILI,
        transcript_sources=["subtitle_track"],
        subtitle_file=source,
    )
    content_run = pipeline.create_run(
        root,
        BILIBILI,
        transcript_sources=["subtitle_track"],
        subtitle_name="../../external/source.ja.srt",
        subtitle_content="Other subtitle",
    )
    assert source.read_text() == "Original subtitle"
    assert (file_run / "subtitle.srt").read_text() == "Original subtitle"
    assert (content_run / "subtitle.srt").read_text() == "Other subtitle"


def test_disk_quota_counts_hard_linked_audio_once(tmp_path, monkeypatch):
    run = tmp_path / "runs/current"
    run.mkdir(parents=True)
    original = run.parent / "previous-audio.m4a"
    original.write_bytes(b"x" * 500)
    os.link(original, run.parent / "reused-audio.m4a")
    monkeypatch.setenv("MEDIA_MAX_DISK_BYTES", "800")
    monkeypatch.setattr("video_report_agent.ingest.shutil.which", lambda _: "/test/yt-dlp")
    downloaded = download_bilibili_video(
        resolve_source(URL),
        run,
        runner=lambda command, **kw: successful_download(run, command),
    )
    assert downloaded.media_path.stat().st_size == 16


def test_cleanup_shared_database_stays_inside_selected_runs_root(tmp_path):
    root = tmp_path / "runs"
    inside, outside = root / "current", tmp_path / "neighbor/other"
    store = Store(tmp_path / "shared.sqlite3")
    for run in (inside, outside):
        run.mkdir(parents=True)
        (run / "audio.wav").write_bytes(b"canary")
        store.register(run, {}, {"state": "FAILED", "finished_at": time.time() - 8 * 86400})
    cleanup_media(root, database_path=store.path)
    assert not (inside / "audio.wav").exists()
    assert (outside / "audio.wav").read_bytes() == b"canary"


@pytest.mark.parametrize(
    "variable",
    [
        "MEDIA_MAX_DURATION_SECONDS",
        "MEDIA_MAX_FILE_BYTES",
        "MEDIA_MAX_DISK_BYTES",
    ],
)
@pytest.mark.parametrize("value", ["0", "-1", "nan", "invalid"])
def test_invalid_limits_are_rejected_before_run_creation(tmp_path, monkeypatch, variable, value):
    monkeypatch.setenv(variable, value)
    with pytest.raises(ValueError, match=variable):
        pipeline.create_run(tmp_path, URL)
    assert not list(tmp_path.iterdir())


def test_limits_are_saved_in_run_input(tmp_path, monkeypatch):
    values = {
        "MEDIA_MAX_DURATION_SECONDS": 60,
        "MEDIA_MAX_FILE_BYTES": 1024,
        "MEDIA_MAX_DISK_BYTES": 4096,
    }
    for key, value in values.items():
        monkeypatch.setenv(key, str(value))
    run = pipeline.create_run(tmp_path, URL)
    assert json.loads((run / "input.json").read_text())["media_limits"] == values


@pytest.mark.parametrize(
    "limit,expected",
    [
        ("MEDIA_MAX_FILE_BYTES", "MEDIA_FILE_TOO_LARGE"),
        ("MEDIA_MAX_DISK_BYTES", "MEDIA_DISK_LIMIT"),
    ],
)
def test_growing_download_is_stopped_and_reaped(tmp_path, monkeypatch, limit, expected):
    monkeypatch.setenv(limit, "100")
    run = tmp_path / "runs/current"
    download = run / "download"
    download.mkdir(parents=True)
    script = (
        "from pathlib import Path; import time; "
        f"Path({str(download / 'source.m4a.part')!r}).write_bytes(b'x' * 101); "
        "time.sleep(20)"
    )
    processes = controlled_workers(monkeypatch, [script], budget=2, checkpoint=1)
    with pytest.raises(UrlIngestError) as caught:
        _run_download(["yt-dlp", URL], run, None)
    assert caught.value.category == expected
    assert len(processes) == 1
    assert all(process.poll() is not None for _, process in processes)


@pytest.mark.parametrize(
    "url,error,code",
    [
        (URL, "This video is private. Please sign in", "DOWNLOAD_LOGIN_REQUIRED"),
        (URL, "This video is members-only", "DOWNLOAD_MEMBERSHIP_REQUIRED"),
        (BILIBILI, "该视频为大会员专享", "DOWNLOAD_MEMBERSHIP_REQUIRED"),
        (
            URL,
            "The uploader has not made this video available in your country",
            "DOWNLOAD_GEO_RESTRICTED",
        ),
        (BILIBILI, "HTTP Error 412: Precondition Failed", "DOWNLOAD_PLATFORM_BLOCKED"),
        (BILIBILI, "[BiliBili] Unable to extract initial state", "DOWNLOAD_PLATFORM_BLOCKED"),
        (BILIBILI, "You have exceeded the rate limit. Try again later",
         "DOWNLOAD_PLATFORM_BLOCKED"),
        (BILIBILI, "This video is only available for registered users", "DOWNLOAD_LOGIN_REQUIRED"),
        (URL, "Sign in to confirm you're not a bot", "DOWNLOAD_PLATFORM_BLOCKED"),
        (URL, "Unable to extract initial state", "DOWNLOAD_ERROR"),
        (URL, "HTTP Error 503", "DOWNLOAD_ERROR"),
    ],
)
def test_download_failure_reasons_use_only_known_error_signals(
    tmp_path,
    monkeypatch,
    url,
    error,
    code,
):
    monkeypatch.setattr("video_report_agent.ingest.shutil.which", lambda _: "/test/yt-dlp")
    with pytest.raises(UrlIngestError) as caught:
        download_bilibili_video(
            resolve_source(url),
            tmp_path,
            runner=lambda *a, **kw: result(
                stderr=error + " https://cdn.invalid/?token=secret", code=1
            ),
        )
    assert caught.value.category == code
    assert "secret" not in str(caught.value)


@pytest.fixture
def whisper(local_stages, monkeypatch, tmp_path):
    calls = {"construct": [], "transcribe": [], "missing": False, "broken": False}

    class Model:
        def __init__(self, model, **kwargs):
            calls["construct"].append(kwargs)

        def transcribe(self, path, *, language, vad_filter=None):
            calls["transcribe"].append({"language": language, "vad_filter": vad_filter})
            if calls["broken"]:
                raise RuntimeError("synthetic inference failure")
            local_stages.append("asr")
            return iter([SimpleNamespace(start=0, end=2, text="Hello world")]), SimpleNamespace(
                language="en",
                duration=3,
                language_probability=0.99,
            )

    monkeypatch.setitem(sys.modules, "faster_whisper", SimpleNamespace(WhisperModel=Model))
    provide_faster_whisper_cache(monkeypatch, tmp_path, available=lambda: not calls["missing"])
    monkeypatch.setattr(pipeline, "transcribe_audio", asr.transcribe_audio)
    return calls


@pytest.mark.parametrize("transcript_only", [False, True])
def test_missing_model_is_explicit_and_retry_reuses_complete_audio(
    tmp_path,
    whisper,
    local_stages,
    transcript_only,
):
    whisper["missing"] = True
    options = dict(
        transcript_mode="auto",
        asr_backend="faster-whisper",
        asr_language="auto",
        transcript_only=transcript_only,
    )
    first = pipeline.create_run(tmp_path, URL, **options)
    failed = pipeline.generate(first)
    assert failed["state"] == "FAILED"
    assert failed["error_category"] == "MODEL_MISSING"
    assert failed["error_code"] == "ASR_MODEL_MISSING"
    assert "scripts/cache_faster_whisper_model.py" in failed["error"]
    assert (first / "download/source.m4a").is_file()
    whisper["missing"] = False
    second = pipeline.create_run(tmp_path, URL, **options)
    finished = pipeline.generate(second)
    assert finished["state"] == ("TRANSCRIPT_READY" if transcript_only else "RENDERED"), finished
    assert finished["download_reused_from"] == first.name
    assert local_stages.count("download") == 1
    assert all(options["local_files_only"] is True for options in whisper["construct"])


def test_transcription_failure_has_its_own_category(tmp_path, whisper):
    whisper["broken"] = True
    run = pipeline.create_run(
        tmp_path, URL, transcript_only=True, asr_backend="faster-whisper", asr_language="en"
    )
    status = pipeline.generate(run)
    assert status["error_category"] == "TRANSCRIPTION_FAILED"
    assert status["error_code"] == "ASR_TRANSCRIPTION_FAILED"


def test_incomplete_cached_snapshot_is_reported_as_missing_model(tmp_path, monkeypatch):
    model = tmp_path / "cached-snapshot"
    model.mkdir()
    (model / "config.json").write_text("{}")
    audio = tmp_path / "audio.wav"
    audio.write_bytes(b"fixture")
    resolved = []

    def construct(*args, **kwargs):
        raise RuntimeError("Unable to open file model.bin")

    def cached_model(name, **kwargs):
        assert kwargs["local_files_only"] is True
        resolved.append(name)
        return str(model)

    monkeypatch.setitem(sys.modules, "faster_whisper", SimpleNamespace(WhisperModel=construct))
    monkeypatch.setitem(
        sys.modules, "faster_whisper.utils", SimpleNamespace(
            _MODELS={"small": "Systran/faster-whisper-small"}, download_model=cached_model,
        )
    )
    with pytest.raises(asr.AsrModelMissingError):
        asr.FasterWhisperBackend("small", tmp_path / "cache").transcribe(audio)
    assert resolved == ["Systran/faster-whisper-small"]


@pytest.mark.parametrize(
    "code,category",
    [
        ("URL_INVALID", "INPUT_REJECTED"),
        ("VIDEO_TOO_LONG", "INPUT_REJECTED"),
        ("MEDIA_FILE_TOO_LARGE", "INPUT_REJECTED"),
        ("MEDIA_DISK_LIMIT", "INPUT_REJECTED"),
        ("DOWNLOAD_PLATFORM_BLOCKED", "DOWNLOAD_FAILED"),
        ("DOWNLOAD_ERROR", "DOWNLOAD_FAILED"),
    ],
)
def test_pipeline_records_rejected_input_and_download_failure_separately(
    tmp_path,
    local_stages,
    monkeypatch,
    code,
    category,
):
    def fail(*args, **kwargs):
        raise UrlIngestError(code, "Synthetic readable reason")

    monkeypatch.setattr(pipeline, "download_bilibili_video", fail)
    run = pipeline.create_run(
        tmp_path, URL, transcript_only=True, asr_backend="faster-whisper", asr_language="en"
    )
    status = pipeline.generate(run)
    assert status["state"] == "FAILED"
    assert status["error_category"] == category
    assert status["error_code"] == code


def test_disk_quota_rejects_before_decoding_and_retry_reuses_audio(
    tmp_path,
    whisper,
    local_stages,
    monkeypatch,
):
    (tmp_path / "previous-media.bin").write_bytes(b"x" * (2 * 1024 * 1024))
    monkeypatch.setenv("MEDIA_MAX_DISK_BYTES", str(1024 * 1024))
    options = dict(transcript_only=True, asr_backend="faster-whisper", asr_language="en")
    first = pipeline.create_run(tmp_path, URL, **options)
    failed = pipeline.generate(first)
    assert failed["error_category"] == "INPUT_REJECTED"
    assert failed["error_code"] == "MEDIA_DISK_LIMIT"
    assert "audio" not in local_stages
    monkeypatch.setenv("MEDIA_MAX_DISK_BYTES", str(10 * 1024 * 1024))
    second = pipeline.create_run(tmp_path, URL, **options)
    status = pipeline.generate(second)
    assert status["state"] == "TRANSCRIPT_READY", status
    assert status["download_reused_from"] == first.name
    assert local_stages.count("download") == 1


def test_vad_is_saved_passed_to_backend_and_separates_reuse(tmp_path, whisper, monkeypatch):
    metadata = []
    for enabled in ("true", "false"):
        monkeypatch.setenv("ASR_VAD_FILTER", enabled)
        run = pipeline.create_run(
            tmp_path, URL, transcript_only=True, asr_backend="faster-whisper", asr_language="auto"
        )
        metadata.append(json.loads((run / "input.json").read_text()))
        status = pipeline.generate(run)
        assert status["state"] == "TRANSCRIPT_READY", status
        assert status.get("asr_reused_from") is None
    assert metadata[0]["asr_parameters"]["vad_filter"] is True
    assert metadata[1]["asr_parameters"]["vad_filter"] is False
    assert cache_key("asr", metadata[0]) != cache_key("asr", metadata[1])
    assert [call["vad_filter"] for call in whisper["transcribe"]] == [True, False]
    assert all(options["local_files_only"] is True for options in whisper["construct"])


def test_vad_defaults_on_and_invalid_boolean_is_rejected(monkeypatch):
    monkeypatch.delenv("ASR_VAD_FILTER", raising=False)
    assert resolve_media_config("faster-whisper")["asr_parameters"]["vad_filter"] is True
    monkeypatch.setenv("ASR_VAD_FILTER", "invalid")
    with pytest.raises(ValueError, match="ASR_VAD_FILTER"):
        resolve_media_config("faster-whisper")


def test_transcription_artifacts_identify_model_detected_language_and_status(tmp_path, whisper):
    run = pipeline.create_run(
        tmp_path, URL, transcript_only=True, asr_backend="faster-whisper", asr_language="auto"
    )
    status = pipeline.generate(run)
    assert status["state"] == "TRANSCRIPT_READY", status
    payload = json.loads((run / "asr.json").read_text())
    assert payload["model"] == "small" and payload["language"] == "en"
    assert payload["segments"][0]["start_ms"] == 0
    assert payload["segments"][0]["end_ms"] == 2000
    assert status["asr_model"] == "small" and status["asr_language"] == "en"
    text = (run / "transcript.md").read_text()
    assert "small" in text and "en" in text and "READY" in text
