import sys
from pathlib import Path

import pytest

import video_report_agent.doctor as doctor
from video_report_agent import asr_paths


def _complete_model(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    for name in ("config.json", "model.bin", "tokenizer.json"):
        (path / name).write_bytes(b"valid")


def test_doctor_resolves_relative_model_from_project_root_outside_cwd(monkeypatch, tmp_path):
    project = tmp_path / "project"
    model = project / "models" / "custom"
    _complete_model(model)
    external = tmp_path / "external"
    external.mkdir()
    monkeypatch.setattr(doctor, "PROJECT_ROOT", project)
    monkeypatch.chdir(external)

    assert doctor._asr_model_ready("./models/custom", tmp_path / "unused-cache")


@pytest.mark.parametrize("missing", ["config.json", "tokenizer.json", "model.bin"])
def test_doctor_requires_every_nonempty_local_model_file(monkeypatch, tmp_path, missing):
    project = tmp_path / "project"
    model = project / "models" / "custom"
    _complete_model(model)
    if missing == "model.bin":
        (model / missing).write_bytes(b"")
    else:
        (model / missing).unlink()
    monkeypatch.setattr(doctor, "PROJECT_ROOT", project)

    assert not doctor._asr_model_ready("./models/custom", tmp_path / "cache")


def test_doctor_uses_installed_faster_whisper_aliases_and_preserves_repo_ids():
    assert asr_paths.faster_whisper_model_repo_id("large") == "Systran/faster-whisper-large-v3"
    assert asr_paths.faster_whisper_model_repo_id("turbo") == (
        "mobiuslabsgmbh/faster-whisper-large-v3-turbo"
    )
    assert asr_paths.faster_whisper_model_repo_id("distil-large-v3") == (
        "Systran/faster-distil-whisper-large-v3"
    )
    assert asr_paths.faster_whisper_model_repo_id("small") == "Systran/faster-whisper-small"
    assert asr_paths.faster_whisper_model_repo_id("owner/custom-model") == "owner/custom-model"


def test_doctor_reads_only_main_snapshot_from_the_selected_cache(monkeypatch, tmp_path):
    project = tmp_path / "project"
    cache = tmp_path / "cache"
    model = cache / "models--Systran--faster-whisper-small" / "snapshots" / "old-revision"
    _complete_model(model)
    monkeypatch.setattr(doctor, "PROJECT_ROOT", project)

    assert not doctor._asr_model_ready("small", cache)

    refs = model.parents[1] / "refs"
    refs.mkdir()
    (refs / "main").write_text("old-revision", encoding="utf-8")
    assert doctor._asr_model_ready("small", cache)
    assert not doctor._asr_model_ready("small", tmp_path / "wrong-cache")


def test_doctor_rejects_incomplete_current_main_snapshot(monkeypatch, tmp_path):
    project = tmp_path / "project"
    cache = tmp_path / "cache"
    model = cache / "models--Systran--faster-whisper-small" / "snapshots" / "main-revision"
    _complete_model(model)
    (model / "tokenizer.json").unlink()
    refs = model.parents[1] / "refs"
    refs.mkdir()
    (refs / "main").write_text("main-revision", encoding="utf-8")
    monkeypatch.setattr(doctor, "PROJECT_ROOT", project)

    assert not doctor._asr_model_ready("small", cache)


def test_doctor_reports_missing_yt_dlp_as_failure(monkeypatch):
    monkeypatch.setattr(
        doctor.shutil,
        "which",
        lambda name: None if name == "yt-dlp" else f"/mock-bin/{name}",
    )
    monkeypatch.setattr(
        doctor.subprocess,
        "run",
        lambda command, **kwargs: type(
            "Completed", (), {"returncode": 0, "stdout": "mock version\n", "stderr": ""}
        )(),
    )
    monkeypatch.setattr(doctor, "_playwright_check", lambda: (True, "mock browser"))
    monkeypatch.setattr(doctor, "_model_check", lambda: (None, "skipped"))
    monkeypatch.setattr(doctor, "_asr_check", lambda: (None, "skipped"))

    row = next(row for row in doctor.run_doctor() if row[0] == "yt-dlp")

    assert row == ("yt-dlp", False, "not found on PATH")


def test_doctor_decodes_synthetic_wav_through_faster_whisper_without_loading_model(monkeypatch):
    pytest.importorskip("faster_whisper.audio", reason="local-asr extra is not installed")
    monkeypatch.setenv("ASR_BACKEND", "faster-whisper")

    ok, detail = doctor._asr_decoder_check()

    assert ok is True
    assert "synthetic WAV decoded" in detail
    assert "no model was loaded" in detail


def test_doctor_reports_faster_whisper_decoder_missing(monkeypatch):
    monkeypatch.setenv("ASR_BACKEND", "faster-whisper")
    monkeypatch.setitem(sys.modules, "faster_whisper", None)
    monkeypatch.setitem(sys.modules, "faster_whisper.audio", None)

    ok, detail = doctor._asr_decoder_check()

    assert ok is False
    assert "audio decoder unavailable" in detail
