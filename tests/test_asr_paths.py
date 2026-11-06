import importlib.util
import os
import subprocess
from pathlib import Path

from asr_fixtures import complete_model_directory

from video_report_agent import asr_paths
from video_report_agent.asr import FasterWhisperBackend


def test_model_and_cache_paths_use_project_root_from_another_working_directory(
    monkeypatch, tmp_path
):
    project = tmp_path / "project"
    model = project / "models" / "custom"
    model.mkdir(parents=True)
    external = tmp_path / "external"
    external.mkdir()
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(asr_paths, "PROJECT_ROOT", project)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.chdir(external)

    assert asr_paths.resolve_faster_whisper_model("./models/custom") == str(model)
    assert asr_paths.resolve_faster_whisper_model("models/custom") == str(model)
    assert asr_paths.resolve_faster_whisper_model("~/weights/custom") == str(
        home / "weights" / "custom"
    )
    assert asr_paths.resolve_faster_whisper_model("Systran/faster-whisper-small") == (
        "Systran/faster-whisper-small"
    )
    assert asr_paths.resolve_faster_whisper_cache_root() == (
        project / ".tooling" / "wsl" / "cache" / "asr"
    )
    assert asr_paths.resolve_faster_whisper_cache_root("~/asr-cache") == home / "asr-cache"


def test_backend_passes_project_rooted_model_and_home_cache_to_faster_whisper(
    monkeypatch, tmp_path
):
    import sys
    from types import SimpleNamespace

    project = tmp_path / "project"
    model = project / "models" / "custom"
    model.mkdir(parents=True)
    audio = tmp_path / "audio.wav"
    audio.write_bytes(b"audio")
    external = tmp_path / "external"
    external.mkdir()
    home = tmp_path / "home"
    home.mkdir()
    calls = {}
    complete_model_directory(model)

    class FakeWhisperModel:
        def __init__(self, model, **kwargs):
            calls["model"] = model
            calls["options"] = kwargs

        def transcribe(self, _path, *, language, vad_filter):
            assert vad_filter is True
            calls["language"] = language
            return iter([SimpleNamespace(start=0, end=1, text="路径测试")]), SimpleNamespace(
                language="zh", duration=1
            )

    monkeypatch.setattr(asr_paths, "PROJECT_ROOT", project)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("FASTER_WHISPER_DOWNLOAD_ROOT", "~/model-cache")
    monkeypatch.chdir(external)
    monkeypatch.setitem(
        sys.modules, "faster_whisper", SimpleNamespace(WhisperModel=FakeWhisperModel)
    )

    result = FasterWhisperBackend("./models/custom").transcribe(audio)

    assert calls["model"] == str(model)
    assert calls["options"] == {
        "device": "cpu",
        "compute_type": "int8",
        "download_root": str(home / "model-cache"),
        "local_files_only": True,
    }
    assert result.model == "./models/custom"


def test_weight_cache_script_uses_project_relative_model_outside_project_cwd(
    monkeypatch, tmp_path
):
    project = tmp_path / "project"
    model = project / "models" / "custom"
    model.mkdir(parents=True)
    (model / "model.bin").write_bytes(b"stub weights")
    external = tmp_path / "external"
    external.mkdir()
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("ASR_MODEL", "./models/custom")
    monkeypatch.setenv("FASTER_WHISPER_DOWNLOAD_ROOT", "~/asr-cache")
    monkeypatch.chdir(external)

    script_path = Path(__file__).parents[1] / "scripts" / "cache_faster_whisper_model.py"
    spec = importlib.util.spec_from_file_location("cache_faster_whisper_model", script_path)
    script = importlib.util.module_from_spec(spec)
    # The script loads the checkout's .env at import; keep it out of the test session.
    monkeypatch.setattr("dotenv.load_dotenv", lambda *args, **kwargs: None)
    spec.loader.exec_module(script)
    script.PROJECT_ROOT = project
    calls = {}

    def fake_model(name, **kwargs):
        calls["name"] = name
        calls["kwargs"] = kwargs

    monkeypatch.setattr(script, "WhisperModel", fake_model)

    assert script.main() == 0
    assert calls == {
        "name": str(model),
        "kwargs": {
            "device": "cpu",
            "compute_type": "int8",
            "download_root": str(home / "asr-cache"),
        },
    }


def test_setup_script_runs_cache_preparation_from_project_root_outside_cwd(tmp_path):
    project = tmp_path / "project"
    external = tmp_path / "external"
    external.mkdir()
    tool_root = project / ".tooling" / "wsl"
    for directory in (
        project / "scripts",
        tool_root / "bin",
        tool_root / "node" / "bin",
        tool_root / "pi" / "node_modules" / ".bin",
        project / ".venv-wsl" / "bin",
    ):
        directory.mkdir(parents=True, exist_ok=True)

    source = Path(__file__).parents[1] / "scripts" / "setup-wsl.sh"
    setup = project / "scripts" / "setup-wsl.sh"
    setup.write_bytes(source.read_bytes())
    log = tmp_path / "setup-calls.log"

    def stub(path: Path, body: str = "") -> None:
        path.write_text(f"#!/usr/bin/env bash\n{body}\n", encoding="utf-8")
        path.chmod(0o755)

    stub(tool_root / "bin" / "uv", 'printf "uv cwd=%s args=%s\\n" "$PWD" "$*" >> "$SMOKE_LOG"')
    stub(project / "scripts" / "install-deno.sh",
         'printf "deno cwd=%s\\n" "$PWD" >> "$SMOKE_LOG"')
    stub(tool_root / "node" / "bin" / "node")
    stub(tool_root / "node" / "bin" / "npm", 'printf "npm cwd=%s\\n" "$PWD" >> "$SMOKE_LOG"')
    stub(tool_root / "bin" / "ffmpeg")
    stub(tool_root / "bin" / "ffprobe")
    stub(
        project / ".venv-wsl" / "bin" / "python",
        'printf "python cwd=%s script=%s\\n" "$PWD" "$1" >> "$SMOKE_LOG"',
    )
    environment = os.environ.copy()
    environment["SMOKE_LOG"] = str(log)
    result = subprocess.run(
        ["bash", str(setup)], cwd=external, env=environment, capture_output=True,
        text=True, timeout=30, check=False
    )

    assert result.returncode == 0, result.stderr
    cache_script = project / "scripts" / "cache_faster_whisper_model.py"
    expected = f"python cwd={project} script={cache_script}"
    calls = log.read_text(encoding="utf-8")
    assert expected in calls
    assert f"deno cwd={project}" in calls
    assert "--locked --inexact --extra local-asr" in calls
    assert calls.index("uv cwd=") < calls.index("deno cwd=") < calls.index("npm cwd=")
