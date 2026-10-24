import json
import sys
from types import SimpleNamespace

import pytest

from video_report_agent import cli, pipeline
from video_report_agent.storage import Store

URL = "https://www.youtube.com/watch?v=ab-cd_ef123"


@pytest.fixture
def local_stages(monkeypatch):
    calls = []

    def forbidden(*args, **kwargs):
        pytest.fail("YouTube must not query Bilibili subtitles or discover a report model")

    def download(source, run, **kwargs):
        assert source.platform == "youtube"
        assert kwargs == {"request_subtitles": False, "audio_only": True}
        calls.append("download")
        folder = run / "download"
        folder.mkdir()
        path = folder / "source.m4a"
        path.write_bytes(b"fixture")
        (folder / "source.info.json").write_text(
            json.dumps({"title": "Fixture", "uploader": "Author", "duration": 3})
        )
        return SimpleNamespace(
            media_path=path, title="Fixture", uploader="Author", attribution="YouTube"
        )

    def audio(path, target):
        calls.append("audio")
        target.write_bytes(b"fixture")
        return SimpleNamespace(path=target, probe=SimpleNamespace(duration_ms=3000))

    def asr(*args, **kwargs):
        assert kwargs["backend"] == "faster-whisper"
        assert kwargs["language"] == "en"
        calls.append("asr")
        segments = [{"ordinal": 0, "start_ms": 0, "end_ms": 2000, "text": "Hello world"}]
        return SimpleNamespace(
            segments=segments,
            to_dict=lambda: {"backend": "faster-whisper", "language": "en", "segments": segments},
        )

    class Runner:
        provider, model = "local", "fixture"

        def __init__(self, **kwargs):
            pass

        async def run(self, run):
            calls.append("report")
            assert "Hello world" in (run / "transcript.md").read_text()
            (run / "report.html").write_text("<html>Fixture</html>")

    def image(run):
        (run / "report.png").write_bytes(b"fixture")

    monkeypatch.setattr(pipeline, "fetch_subtitles", forbidden)
    monkeypatch.setattr(cli, "resolve_model_selection", forbidden)
    monkeypatch.setattr(pipeline, "download_bilibili_video", download)
    monkeypatch.setattr(pipeline, "extract_audio", audio)
    monkeypatch.setattr(pipeline, "transcribe_audio", asr)
    monkeypatch.setattr(pipeline, "PiRunner", Runner)
    monkeypatch.setattr(pipeline, "render_report_image", image)
    return calls


@pytest.mark.parametrize("transcript_only", [False, True])
@pytest.mark.parametrize("mode", ["auto", "asr-only", "selected"])
def test_youtube_pipeline_and_transcript_reuse(tmp_path, local_stages, transcript_only, mode):
    runs = []
    for _ in range(2):
        run = pipeline.create_run(
            tmp_path,
            URL,
            transcript_mode="asr-only" if mode == "selected" else mode,
            transcript_sources=["asr"] if mode == "selected" else None,
            transcript_only=transcript_only,
            asr_backend="faster-whisper",
            asr_language="en",
        )
        runs.append(run)
        result = pipeline.generate(run)
        assert result["state"] == ("TRANSCRIPT_READY" if transcript_only else "RENDERED"), result
        assert "Hello world" in (run / "transcript.md").read_text()
    assert result["transcript_reused_from"] == runs[0].name
    assert local_stages == (
        ["download", "audio", "asr"] if transcript_only
        else ["download", "audio", "asr", "report", "report"]
    )
    with Store(tmp_path / "state.sqlite3", initialize=False).read() as db:
        row = db.execute("SELECT platform,external_id,part FROM videos").fetchone()
        assert tuple(row) == ("youtube", "ab-cd_ef123", None)


@pytest.mark.parametrize(
    "sources", [["subtitle_track"], ["bilibili_ai"], ["asr", "subtitle_track"]]
)
def test_youtube_rejects_bilibili_sources_before_creating_run(tmp_path, sources):
    with pytest.raises(ValueError, match="YouTube.*ASR.*subtitle_track.*bilibili_ai"):
        pipeline.create_run(tmp_path, URL, transcript_sources=sources)
    assert not list(tmp_path.iterdir())


def test_transcribe_cli_does_not_require_report_model(tmp_path, monkeypatch, local_stages):
    monkeypatch.setattr(cli, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(cli, "generate", pipeline.generate)
    monkeypatch.setattr(
        sys, "argv",
        ["video-report", "--runs", str(tmp_path), "transcribe", URL,
         "--asr-backend", "faster-whisper", "--asr-language", "en"],
    )
    assert cli.main() == 0
    assert local_stages == ["download", "audio", "asr"]


def test_youtube_reuses_media_and_asr_across_transcript_modes(tmp_path, local_stages):
    runs = []
    for mode in ("asr-only", "auto"):
        run = pipeline.create_run(
            tmp_path, URL, transcript_mode=mode, transcript_only=True,
            asr_backend="faster-whisper", asr_language="en",
        )
        runs.append(run)
        result = pipeline.generate(run)
        assert result["state"] == "TRANSCRIPT_READY", result
    assert result["download_reused_from"] == runs[0].name
    assert result["asr_reused_from"] == runs[0].name
    assert local_stages == ["download", "audio", "asr", "audio"]
