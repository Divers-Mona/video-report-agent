import importlib
import json
import time
from types import SimpleNamespace

import pytest
from test_download_recovery import controlled_workers
from test_youtube_pipeline import local_stages as local_stages

from video_report_agent import asr, doctor, pipeline
from video_report_agent.ingest import UrlIngestError, download_bilibili_video, resolve_source
from video_report_agent.retention import cleanup_media
from video_report_agent.storage import Store

URL = "https://www.youtube.com/watch?v=jNQXAC9IVRw"


@pytest.fixture
def real_constructor(local_stages, monkeypatch):
    fw = pytest.importorskip("faster_whisper")
    module = importlib.import_module("faster_whisper.transcribe")
    hub = importlib.import_module("huggingface_hub")
    tokenizer = module.tokenizers.Tokenizer
    calls = {"remote": [], "weights": [], "inference": []}
    original_snapshot = hub.snapshot_download

    def snapshot(*args, **kwargs):
        assert kwargs["local_files_only"] is True
        return original_snapshot(*args, **kwargs)

    def weights(path, **kwargs):
        calls["weights"].append(path)
        return SimpleNamespace(is_multilingual=True)

    def remote(*args, **kwargs):
        calls["remote"].append(args)
        raise RuntimeError("Remote tokenizer acquisition is forbidden")

    def transcribe(self, path, *, language, vad_filter):
        calls["inference"].append(path)
        assert language is None and vad_filter is True
        return iter([SimpleNamespace(start=0, end=2, text="Hello world")]), SimpleNamespace(
            language="en",
            duration=3,
        )

    monkeypatch.setenv("HF_HUB_OFFLINE", "1")
    monkeypatch.setattr(hub, "snapshot_download", snapshot)
    monkeypatch.setattr(module.ctranslate2.models, "Whisper", weights)
    monkeypatch.setattr(
        module.tokenizers,
        "Tokenizer",
        SimpleNamespace(
            from_pretrained=remote,
            from_file=tokenizer.from_file,
            from_buffer=tokenizer.from_buffer,
        ),
    )
    monkeypatch.setattr(fw.WhisperModel, "transcribe", transcribe)
    monkeypatch.setattr(pipeline, "transcribe_audio", asr.transcribe_audio)
    return calls, tokenizer, module.tokenizers.models.WordLevel


def model_directory(tmp_path, tokenizer, word_level, origin):
    cache = tmp_path / "model-cache"
    if origin == "directory":
        model = tmp_path / "local-model"
        reference = str(model)
    else:
        model = cache / "models--Systran--faster-whisper-small/snapshots/revision"
        refs = model.parents[1] / "refs"
        refs.mkdir(parents=True)
        (refs / "main").write_text("revision")
        reference = "small"
    model.mkdir(parents=True)
    (model / "config.json").write_text("{}")
    (model / "model.bin").write_bytes(b"synthetic weights; no CTranslate2 load")
    tokenizer(word_level({"[UNK]": 0}, unk_token="[UNK]")).save(str(model / "tokenizer.json"))
    return model, cache, reference


@pytest.mark.parametrize("origin", ["directory", "cache"])
@pytest.mark.parametrize("missing", ["tokenizer.json", "model.bin", "config.json"])
@pytest.mark.parametrize("empty", [False, True])
def test_incomplete_model_fails_before_real_constructor_remote_fallback(
    tmp_path,
    monkeypatch,
    real_constructor,
    origin,
    missing,
    empty,
):
    calls, tokenizer, word_level = real_constructor
    model, cache, reference = model_directory(tmp_path, tokenizer, word_level, origin)
    if empty:
        (model / missing).write_bytes(b"")
    else:
        (model / missing).unlink()
    monkeypatch.setenv("FASTER_WHISPER_DOWNLOAD_ROOT", str(cache))
    assert doctor._asr_model_ready(reference, cache) is False
    run = pipeline.create_run(
        tmp_path / "runs",
        URL,
        transcript_only=True,
        transcript_mode="auto",
        asr_backend="faster-whisper",
        asr_model=reference,
        asr_language="auto",
    )
    status = pipeline.generate(run)
    assert calls["remote"] == [], "No tokenizer network fallback may be entered"
    assert calls["weights"] == [], "Incomplete models must be rejected before engine construction"
    assert calls["inference"] == []
    assert status["state"] == "FAILED"
    assert status["error_code"] == "ASR_MODEL_MISSING"
    assert status["error_category"] == "MODEL_MISSING"


@pytest.mark.parametrize("origin", ["directory", "cache"])
def test_complete_model_uses_real_constructor_without_remote_fetch(
    tmp_path,
    monkeypatch,
    real_constructor,
    origin,
):
    calls, tokenizer, word_level = real_constructor
    model, cache, reference = model_directory(tmp_path, tokenizer, word_level, origin)
    monkeypatch.setenv("FASTER_WHISPER_DOWNLOAD_ROOT", str(cache))
    assert doctor._asr_model_ready(reference, cache) is True
    run = pipeline.create_run(
        tmp_path / "runs",
        URL,
        transcript_only=True,
        transcript_mode="auto",
        asr_backend="faster-whisper",
        asr_model=reference,
        asr_language="auto",
    )
    status = pipeline.generate(run)
    assert status["state"] == "TRANSCRIPT_READY", status
    assert calls["remote"] == []
    assert calls["weights"] == [str(model)]
    assert len(calls["inference"]) == 1


@pytest.mark.parametrize("filename", ["source.m4a.part", "source.f140.m4a.part-Frag0"])
@pytest.mark.parametrize("age", [6, 8])
@pytest.mark.parametrize(
    "limit,code",
    [
        ("MEDIA_MAX_FILE_BYTES", "MEDIA_FILE_TOO_LARGE"),
        ("MEDIA_MAX_DISK_BYTES", "MEDIA_DISK_LIMIT"),
    ],
)
def test_limit_stopped_media_partials_follow_seven_day_retention(
    tmp_path,
    monkeypatch,
    filename,
    age,
    limit,
    code,
):
    root = tmp_path / "runs"
    run = root / "current"
    download = run / "download"
    download.mkdir(parents=True)
    monkeypatch.setenv(limit, "100")
    monkeypatch.setenv("MEDIA_KEEP_LAST", "0")
    monkeypatch.setenv("MEDIA_MAX_AGE_DAYS", "7")
    monkeypatch.setattr("video_report_agent.ingest.shutil.which", lambda _: "/test/yt-dlp")
    partial = download / filename
    script = (
        "from pathlib import Path; import time; "
        f"Path({str(partial)!r}).write_bytes(b'x' * 101); time.sleep(20)"
    )
    processes = controlled_workers(monkeypatch, [script], budget=2, checkpoint=1)
    with pytest.raises(UrlIngestError) as caught:
        download_bilibili_video(resolve_source(URL), run)
    assert caught.value.category == code
    assert len(processes) == 1
    assert all(process.poll() is not None for _, process in processes)
    assert partial.stat().st_size == 101
    store = Store(root / "state.sqlite3")
    store.register(run, {}, {"state": "FAILED", "finished_at": time.time() - age * 86400})
    preserved = {
        "transcript.md": "transcription",
        "failure.log": "diagnostic",
        "download.log": "download diagnostic",
        "download/source.zh.srt": "subtitles",
        "download/source.zh.srt.part": "subtitle partial",
        "download/source.info.json": json.dumps({"title": "metadata"}),
        "download/source.debug.txt.part": "diagnostic partial",
    }
    for name, body in preserved.items():
        (run / name).write_text(body)
    outside = tmp_path / "other-root/foreign"
    outside.mkdir(parents=True)
    canary = outside / filename
    canary.write_bytes(b"foreign data")
    store.register(outside, {}, {"state": "FAILED", "finished_at": time.time() - 8 * 86400})
    cleanup_media(root, database_path=store.path)
    assert partial.exists() is (age < 7)
    assert canary.read_bytes() == b"foreign data"
    for name, body in preserved.items():
        assert (run / name).read_text() == body
