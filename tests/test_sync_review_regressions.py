import asyncio
import copy
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_pi import RecoveryProcess, provider_attempt

from video_report_agent import pi, pipeline
from video_report_agent.ingest import resolve_source
from video_report_agent.local_models import local_provider_config
from video_report_agent.retention import cleanup_cancelled_run
from video_report_agent.reuse import reuse_download
from video_report_agent.storage import Store
from video_report_agent.task_container import snapshot_models

BILIBILI = "https://www.bilibili.com/video/BV1qxn16yEvP/"
YOUTUBE = "https://www.youtube.com/watch?v=jNQXAC9IVRw"
JAPANESE = "私は東京の図書館で勉強します。"
SIMPLIFIED_JAPANESE = "私は东京の図书馆で勉强します。"


@pytest.mark.parametrize("url", [BILIBILI, YOUTUBE])
@pytest.mark.parametrize("mode", ["asr-only", "auto", "selected"])
def test_cancelled_audio_can_be_reused_by_retry(tmp_path, url, mode):
    options = {
        "transcript_mode": "asr-only" if mode == "selected" else mode,
        "transcript_sources": ["asr"] if mode == "selected" else None,
        "asr_backend": "faster-whisper",
    }
    run = pipeline.create_run(tmp_path, url, **options)
    store = Store(tmp_path / "state.sqlite3", initialize=False)
    download = run / "download"
    download.mkdir()
    media = download / "source.m4a"
    media.write_bytes(b"complete audio")
    (download / "source.m4a.part").write_bytes(b"partial")
    store.artifact(run.name, "download/source.m4a", media, "media")
    store.document(run.name, "download/source.info.json", json.dumps({
        "title": "Fixture", "uploader": "Author", "duration": 3,
    }))

    cleanup_cancelled_run(run)
    assert media.read_bytes() == b"complete audio"
    assert not (download / "source.m4a.part").exists()
    with store.read() as db:
        assert db.execute(
            "SELECT available FROM artifacts WHERE run_id=? AND name=?",
            (run.name, "download/source.m4a"),
        ).fetchone()[0] == 1
    retry = pipeline.create_run(tmp_path, url, **options)
    reused, producer = reuse_download(
        resolve_source(url), retry, request_subtitles=False, audio_only=True,
    )
    assert producer == run.name
    assert reused.media_path.read_bytes() == b"complete audio"


@pytest.mark.parametrize("isolation", ["local", "docker"])
def test_local_recovery_models_survive_runner_startup(tmp_path, monkeypatch, isolation):
    shared = tmp_path / "shared"
    shared.mkdir()
    monkeypatch.setattr(pi, "PI_AGENT_DIR", shared)
    monkeypatch.setenv("PI_API_KEY", "synthetic-offline-key")
    monkeypatch.setenv("PI_BASE_URL", "http://127.0.0.1:1/v1")
    provider = local_provider_config("primary")
    backup = {**copy.deepcopy(provider["models"][0]), "id": "backup", "name": "Backup",
              "maxTokens": 1024}
    provider["models"].append(backup)
    (shared / "models.json").write_text(json.dumps({"providers": {"local": provider}}))
    selections = [{"provider": "local", "model": name} for name in ("primary", "backup")]
    # The real offline Pi resolver confirms that the initial definitions are valid.
    snapshot_models(tmp_path / "before", shared, selections)
    run = tmp_path / "run"
    run.mkdir()
    (run / "transcript.md").write_text("Synthetic transcript")
    (run / "input.json").write_text("{}")
    process = RecoveryProcess([
        provider_attempt("local", "primary", 1, stop_reason="error",
                         error="data_inspection_failed: input rejected"),
        provider_attempt("local", "backup", 2, stop_reason="stop"),
    ])
    process.returncode, process.pid = 0, os.getpid()
    resolved = []

    async def start(*command, **kwargs):
        if isolation == "local":
            agent = Path(kwargs["env"]["PI_CODING_AGENT_DIR"])
            target = tmp_path / "local-resolved"
            snapshot_models(target, agent, selections)
            workspace = run
        else:
            target = run / ".generation-config"
            workspace = run / ".generation"
        models = json.loads((target / "models.json").read_text())["providers"]["local"]["models"]
        resolved.extend(model["id"] for model in models)
        (workspace / "report.html").write_text("<html><body>Fixture</body></html>")
        return process

    monkeypatch.setattr(pi.asyncio, "create_subprocess_exec", start)
    monkeypatch.setattr(pi, "launch_command", lambda *args: ["synthetic-docker-process"])
    result = asyncio.run(pi.PiRunner(
        provider="local", model="primary", api_key="synthetic-offline-key", isolation=isolation,
        timeout=900, model_recovery={"alternate": selections[1]},
    ).run(run))
    assert result.read_text() == "<html><body>Fixture</body></html>"
    assert resolved == ["primary", "backup"]
    assert process.model_commands[0]["modelId"] == "backup"
    models = json.loads((shared / "models.json").read_text())["providers"]["local"]["models"]
    assert next(model for model in models if model["id"] == "backup") == backup
    events = [json.loads(line) for line in (run / "model-attempts.jsonl").read_text().splitlines()]
    assert events[-1]["success"] is True
    assert events[-1]["completed_model"] == "backup"


@pytest.fixture
def imported_subtitles(monkeypatch):
    monkeypatch.setenv("ASR_BACKEND", "paraformer")
    monkeypatch.delenv("DASHSCOPE_API_KEY", raising=False)
    monkeypatch.delenv("ASR_LANGUAGE", raising=False)

    def forbidden(*args, **kwargs):
        pytest.fail("Imported subtitles must not start media, ASR or report work")

    for name in ("download_bilibili_video", "extract_audio", "transcribe_audio", "PiRunner"):
        monkeypatch.setattr(pipeline, name, forbidden)
    monkeypatch.setattr(pipeline, "fetch_subtitles", lambda *a, **kw: {
        "title": "Fixture", "uploader": "Author", "attribution": "Bilibili",
        "duration_ms": 3000, "sources": {},
    })
    parsed = []

    def load(path, **kwargs):
        parsed.append(path)
        return [SimpleNamespace(start=0, end=2000, text=Path(path).read_text().splitlines()[-1])]

    monkeypatch.setitem(sys.modules, "pysubs2", SimpleNamespace(load=load))
    return parsed


@pytest.mark.parametrize("importer", ["file", "content"])
@pytest.mark.parametrize("filename,language,text,expected", [
    ("source.ja.srt", "ja", JAPANESE, JAPANESE),
    ("source.ja.srt", None, JAPANESE, JAPANESE),
    ("source.ja.srt", "zh", JAPANESE, JAPANESE),
    ("source.srt", "ja", JAPANESE, JAPANESE),
    ("source.zh.srt", "ja", "我在圖書館學習。", "我在图书馆学习。"),
    ("source.srt", None, "我在圖書館學習。", "我在图书馆学习。"),
])
def test_imported_subtitle_language_survives_storage_export_and_reuse(
    tmp_path, imported_subtitles, importer, filename, language, text, expected,
):
    body = f"1\n00:00:00,000 --> 00:00:02,000\n{text}\n"
    subtitle = tmp_path / filename
    subtitle.write_text(body)
    arguments = {"subtitle_file": subtitle} if importer == "file" else {
        "subtitle_name": filename, "subtitle_content": body,
    }
    runs = tmp_path / "runs"
    created = []
    for _ in range(2):
        run = pipeline.create_run(
            runs, BILIBILI, transcript_sources=["subtitle_track"], transcript_only=True,
            asr_language=language, **arguments,
        )
        created.append(run)
        assert (run / "subtitle.srt").read_text() == body
        store = Store(runs / "state.sqlite3", initialize=False)
        assert store.run(run.name)["input"] == json.loads((run / "input.json").read_text())
        status = pipeline.generate(run)
        assert status["state"] == "TRANSCRIPT_READY", status
        units = [
            json.loads(line)
            for line in (run / "canonical-transcript.jsonl").read_text().splitlines()
        ]
        assert units[0]["canonical_text"] == expected
        assert expected in (run / "transcript.md").read_text()
    assert status["transcript_reused_from"] == created[0].name
    assert len(imported_subtitles) == 1


def test_imported_subtitle_language_isolates_cached_transcripts(tmp_path, imported_subtitles):
    created = []
    for language, expected in (("zh", SIMPLIFIED_JAPANESE), ("ja", JAPANESE), ("ja", JAPANESE)):
        run = pipeline.create_run(
            tmp_path, BILIBILI, transcript_sources=["subtitle_track"], transcript_only=True,
            subtitle_name="source.srt", subtitle_content=JAPANESE, asr_language=language,
        )
        status = pipeline.generate(run)
        assert status["state"] == "TRANSCRIPT_READY", status
        units = [
            json.loads(line)
            for line in (run / "canonical-transcript.jsonl").read_text().splitlines()
        ]
        assert units[0]["canonical_text"] == expected
        if len(created) < 2:
            assert status["transcript_reused_from"] is None
        else:
            assert status["transcript_reused_from"] == created[1].name
        created.append(run)
    assert len(imported_subtitles) == 2
