import json
import sys
from types import SimpleNamespace

import pytest

from video_report_agent import cli, pipeline, retention, whisper_cpp
from video_report_agent.asr import AsrError, AsrModelMissingError, transcribe_audio
from video_report_agent.media_config import resolve_media_config
from video_report_agent.reuse import cache_key
from video_report_agent.storage import Store

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="HIP CLI runs in WSL/Linux")


@pytest.fixture
def local_cpp(tmp_path, monkeypatch):
    monkeypatch.setattr(whisper_cpp, "PROJECT_ROOT", tmp_path)
    monkeypatch.delenv("ASR_MODEL", raising=False)
    monkeypatch.delenv("WHISPER_CPP_BINARY", raising=False)
    model = tmp_path / "models/whisper-cpp/ggml-large-v3-turbo.bin"
    model.parent.mkdir(parents=True)
    model.write_bytes(b"fixture weights")
    binary = tmp_path / ".tooling/wsl/whisper.cpp/build/bin/whisper-cli"
    binary.parent.mkdir(parents=True)
    binary.write_text(
        f"#!{sys.executable}\n"
        "import json, pathlib, sys\n"
        "args = sys.argv[1:]\n"
        "assert args[args.index('--language') + 1] in ('zh', 'auto')\n"
        "print('whisper_backend_init_gpu: using ROCm0 backend', file=sys.stderr)\n"
        "print('whisper_model_load: ROCm0 total size = 1600.00 MB', file=sys.stderr)\n"
        "p = pathlib.Path(args[args.index('--output-file')+1]).with_suffix('.json')\n"
        "p.write_text(json.dumps({'result': {'language': 'zh'}, 'transcription': ["
        "{'offsets': {'from': 100, 'to': 900}, 'text': '止损测试'}]}))\n",
        encoding="utf-8",
    )
    binary.chmod(0o755)
    audio = tmp_path / "audio.wav"
    audio.write_bytes(b"fixture audio")
    return tmp_path, model, binary, audio


def test_cpp_subprocess_normalizes_offsets_and_records_gpu(local_cpp):
    root, _, _, audio = local_cpp
    config = resolve_media_config("whisper-cpp")
    result = transcribe_audio(
        audio, backend="whisper-cpp", model=config["asr_model"], language="auto",
        parameters=config["asr_parameters"], task_path=root / "asr-task.json",
    )
    assert result.backend == "whisper-cpp"
    assert result.language == "zh"
    assert result.raw_result["gpu_backend"] == "ROCm0"
    assert (result.segments[0].start_ms, result.segments[0].end_ms) == (100, 900)
    assert result.segments[0].text == "止损测试"
    assert (root / "whisper-cpp.log").is_file()
    assert (root / "whisper-cpp.raw.json").is_file()
    assert not list(root.glob("whisper-cpp-*/"))


@pytest.mark.parametrize(
    "mode", ["cpu", "init_failure", "exit", "json", "timestamp", "overlap"]
)
def test_cpp_rejects_failed_or_invalid_output(local_cpp, mode):
    _, _, _, audio = local_cpp
    config = resolve_media_config("whisper-cpp")

    def run(command, **kwargs):
        kwargs["stderr"].write(
            b"CPU only" if mode == "cpu" else
            b"whisper_backend_init_gpu: using ROCm0 backend\n"
            b"whisper_model_load: ROCm0 total size = 1600.00 MB\n"
        )
        if mode == "init_failure":
            kwargs["stderr"].write(
                b"whisper_backend_init_gpu: failed to initialize ROCm0 backend\n"
                b"whisper_backend_init: using CPU backend\n"
            )
        row = {"offsets": {"from": 0, "to": 900}, "text": "测试"}
        if mode == "timestamp":
            row["offsets"]["from"] = "invalid"
        payload = {"result": {"language": "zh"}, "transcription": [row]}
        if mode == "overlap":
            payload["transcription"].append(row)
        prefix = command[command.index("--output-file") + 1]
        from pathlib import Path
        Path(prefix).with_suffix(".json").write_text(
            "invalid" if mode == "json" else json.dumps(payload)
        )
        return SimpleNamespace(returncode=3 if mode == "exit" else 0)

    from unittest.mock import patch
    with patch.object(whisper_cpp.subprocess, "run", run), pytest.raises(AsrError):
        transcribe_audio(
            audio, backend="whisper-cpp", model=config["asr_model"],
            parameters=config["asr_parameters"],
        )
    assert audio.with_name("whisper-cpp.raw.json").is_file()


def test_cpp_partial_utf8_segment_text_has_visible_marker_and_exact_provenance():
    raw = (
        b'{"result":{"language":"zh"},"transcription":[{"offsets":'
        b'{"from":1171960,"to":1179960},"text":"'
        + "换一种说法大家就会觉得更熟".encode("utf-8")
        + b'\xe6\x82"}]}'
    )
    with pytest.raises(UnicodeDecodeError):
        raw.decode("utf-8")
    payload, rows, repairs = whisper_cpp.parse_whisper_cpp_json(raw)
    assert payload["transcription"][0]["text"] == "换一种说法大家就会觉得更熟\ufffd"
    assert rows == [{"start": 1171.96, "end": 1179.96, "text": "换一种说法大家就会觉得更熟\ufffd"}]
    assert len(repairs) == 1
    repair = repairs[0]
    assert repair["raw_segment_ordinal"] == 0
    assert (repair["start_ms"], repair["end_ms"]) == (1171960, 1179960)
    assert repair["invalid_bytes_hex"] == "e682"
    assert raw[repair["raw_json_byte_start"]:repair["raw_json_byte_end"]] == b"\xe6\x82"
    assert repair["text_character_offset"] == len("换一种说法大家就会觉得更熟")


@pytest.mark.parametrize("raw", [
    b'{"result":{"language":"z\xe6\x82"},"transcription":[]}',
    b'{"transcrip\xe6\x82tion":[]}',
    b'{"transcription":[{"offsets":{"from":0,"to":1},"text":"\\ud800"}]}',
    b'{"transcription":[{"offsets":{"from":0,"to":1},"text":"ok","x":"\xff"}]}',
])
def test_cpp_does_not_repair_nontext_fields_or_unpaired_unicode_escapes(raw):
    with pytest.raises(AsrError):
        whisper_cpp.parse_whisper_cpp_json(raw)


def test_cpp_model_identity_changes_cache_and_blocks_changed_run(local_cpp):
    _, model, _, audio = local_cpp
    config = resolve_media_config("whisper-cpp")
    first = cache_key("asr", {"video_id": "abc", **config})
    model.write_bytes(b"changed weights")
    assert cache_key("asr", {"video_id": "abc", **resolve_media_config("whisper-cpp")}) != first
    with pytest.raises(AsrError, match="changed after run creation"):
        transcribe_audio(
            audio, backend="whisper-cpp", model=config["asr_model"],
            parameters=config["asr_parameters"],
        )
    model.unlink()
    with pytest.raises(AsrModelMissingError, match="missing"):
        resolve_media_config("whisper-cpp")


@pytest.mark.parametrize("damaged", [False, True])
@pytest.mark.parametrize("reuse_kind", ["asr", "transcript"])
def test_cpp_transcribe_cli_pipeline_and_cache_after_cancel(
    local_cpp, monkeypatch, damaged, reuse_kind
):
    root, _, _, _ = local_cpp
    calls = []

    def download(source, run, **kwargs):
        calls.append("download")
        folder = run / "download"
        folder.mkdir()
        media = folder / "source.m4a"
        media.write_bytes(b"audio")
        (folder / "source.info.json").write_text(
            json.dumps({"title": "Fixture", "uploader": "Author", "duration": 1})
        )
        return SimpleNamespace(
            media_path=media, title="Fixture", uploader="Author", attribution="YouTube"
        )

    def audio(path, target):
        target.write_bytes(b"audio")
        return SimpleNamespace(path=target, probe=SimpleNamespace(duration_ms=1000))

    original_bytes = (
        b'{"result":{"language":"zh"},"transcription":['
        b'{"offsets":{"from":100,"to":900},"text":"'
        + "止损测试".encode() + b'\xe6\x82"}]}'
    )

    def damaged_cli(command, **kwargs):
        from pathlib import Path

        kwargs["stderr"].write(
            b"whisper_backend_init_gpu: using ROCm0 backend\n"
            b"whisper_model_load: ROCm0 total size = 1600.00 MB\n"
        )
        Path(command[command.index("--output-file") + 1]).with_suffix(".json").write_bytes(
            original_bytes
        )
        return SimpleNamespace(returncode=0)

    if damaged:
        monkeypatch.setattr(whisper_cpp.subprocess, "run", damaged_cli)

    monkeypatch.setattr(pipeline, "download_bilibili_video", download)
    monkeypatch.setattr(pipeline, "extract_audio", audio)
    monkeypatch.setattr(cli, "generate", pipeline.generate)
    monkeypatch.setattr(cli, "PROJECT_ROOT", root)
    monkeypatch.setattr(sys, "argv", [
        "video-report", "--runs", str(root / "runs"), "transcribe",
        "https://www.youtube.com/watch?v=ab-cd_ef123", "--asr-backend", "whisper-cpp",
        "--transcript-mode", "asr-only",
    ])
    assert cli.main() == 0
    source = next(p for p in (root / "runs").iterdir() if p.is_dir())
    store = Store(root / "runs/state.sqlite3", initialize=False)
    status = json.loads((source / "status.json").read_text())
    with store.transaction() as db:
        db.execute(
            "UPDATE runs SET status_json=? WHERE id=?",
            (json.dumps({**status, "state": "CANCELLED"}), source.name),
        )
    with monkeypatch.context() as cleanup_patch:
        if reuse_kind == "asr":
            cleanup_patch.setattr(retention, "validated_transcript", lambda *args: None)
        retention.cleanup_cancelled_run(source)
    assert (source / "whisper-cpp.raw.json").is_file()
    assert json.loads((source / "status.json").read_text())["state"] == "CANCELLED"
    # Cache evidence must survive even if the producer's diagnostic file is lost.
    (source / "whisper-cpp.raw.json").unlink()

    def forbidden(*args, **kwargs):
        pytest.fail("Cached ASR/transcript must not invoke inference again")

    monkeypatch.setattr(pipeline, "transcribe_audio", forbidden)
    if reuse_kind == "asr":
        sys.argv[-1] = "auto"  # Different transcript identity; same ASR identity.
    assert cli.main() == 0
    runs = [p for p in (root / "runs").iterdir() if p.is_dir()]
    assert len(runs) == 2
    assert calls == ["download"]
    target = next(p for p in runs if p != source)
    assert "止损测试" in (target / "transcript.md").read_text()
    status = json.loads((target / "status.json").read_text())
    assert status["state"] == "TRANSCRIPT_READY"
    assert status[f"{reuse_kind}_reused_from"] == source.name
    asr = json.loads((target / "asr.json").read_text())
    if damaged:
        assert (target / "whisper-cpp.raw.json").read_bytes() == original_bytes
        assert "止损测试\ufffd" in (target / "transcript.md").read_text()
        repair = asr["raw_result"]["utf8_repairs"][0]
        assert original_bytes[
            repair["raw_json_byte_start"]:repair["raw_json_byte_end"]
        ].hex() == repair["invalid_bytes_hex"]
    else:
        assert "raw_output_base64" not in asr["raw_result"]


@pytest.mark.parametrize("captions_ready", [True, False])
def test_auto_only_requires_cpp_files_when_route_reaches_asr(
    local_cpp, monkeypatch, captions_ready
):
    root, model, binary, _ = local_cpp
    model.unlink()
    binary.unlink()
    monkeypatch.setattr(pipeline, "fetch_subtitles", lambda *a, **kw: {
        "video_id": "bilibili-BV1qxn16yEvP-p1", "bvid": "BV1qxn16yEvP", "cid": 12,
        "attribution": "Bilibili",
        "title": "Fixture", "uploader": "Author", "duration_ms": 1000,
        "sources": {
            "subtitle_track": {
                "status": "READY" if captions_ready else "ABSENT", "count": 1,
                **({"body": [{"from": 0, "to": 1, "content": "字幕正文"}]}
                   if captions_ready else {}),
            },
            "bilibili_ai": {"status": "ABSENT", "count": 0},
        },
    })

    def forbidden(*args, **kwargs):
        pytest.fail("No media or inference may run before checking the selected source")

    monkeypatch.setattr(pipeline, "download_bilibili_video", forbidden)
    run = pipeline.create_run(
        root / "runs", "https://www.bilibili.com/video/BV1qxn16yEvP/",
        transcript_mode="auto", transcript_only=True, asr_backend="whisper-cpp",
    )
    metadata = json.loads((run / "input.json").read_text())
    assert metadata["asr_parameters"]["model_path_size"] is None
    status = pipeline.generate(run)
    if captions_ready:
        assert status["state"] == "TRANSCRIPT_READY", status
        assert status["asr_performed"] is False
        assert "字幕正文" in (run / "transcript.md").read_text()
    else:
        assert status["state"] == "FAILED", status
        assert "missing or empty" in status["error"]
    assert not (run / "asr.json").exists()
