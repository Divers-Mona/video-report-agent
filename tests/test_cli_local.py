import sys

import pytest

import video_report_agent.cli as cli
import video_report_agent.local_models as local_models


def _isolate_cli(monkeypatch, tmp_path):
    monkeypatch.setattr(cli, "PROJECT_ROOT", tmp_path)
    (tmp_path / ".env").write_text("", encoding="utf-8")


def test_generate_cli_persists_only_the_local_model_selection(monkeypatch, tmp_path, capsys):
    _isolate_cli(monkeypatch, tmp_path)
    monkeypatch.setenv("PI_PROVIDER", "local")
    monkeypatch.delenv("PI_MODEL", raising=False)
    monkeypatch.setenv("PI_API_KEY", "sensitive-test-value")
    monkeypatch.setattr(
        local_models,
        "discover_models",
        lambda **kwargs: pytest.fail("--model skips /models discovery"),
    )
    captured = {}
    monkeypatch.setattr(
        cli,
        "create_run",
        lambda root, url, **kwargs: captured.update(url=url, **kwargs) or tmp_path / "run",
    )
    monkeypatch.setattr(cli, "generate", lambda run, **kwargs: {"state": "RENDERED"})
    monkeypatch.setattr(
        sys,
        "argv",
        ["video-report", "generate", "https://www.bilibili.com/video/BV1cZ8x6sEhF/",
         "--model", "explicit-model"],
    )

    assert cli.main() == 0
    assert captured["model_selection"] == {"provider": "local", "model": "explicit-model"}
    assert "sensitive-test-value" not in capsys.readouterr().out


def test_generate_cli_discovers_single_model_and_requires_choice_for_multiple(
    monkeypatch, tmp_path
):
    _isolate_cli(monkeypatch, tmp_path)
    monkeypatch.setenv("PI_PROVIDER", "local")
    monkeypatch.delenv("PI_MODEL", raising=False)
    monkeypatch.setattr(local_models, "discover_models", lambda **kwargs: ["auto-id"])
    captured = {}
    monkeypatch.setattr(
        cli,
        "create_run",
        lambda root, url, **kwargs: captured.update(kwargs) or tmp_path / "run",
    )
    monkeypatch.setattr(cli, "generate", lambda run, **kwargs: {"state": "RENDERED"})
    monkeypatch.setattr(
        sys,
        "argv",
        ["video-report", "generate", "https://www.bilibili.com/video/BV1cZ8x6sEhF/"],
    )
    assert cli.main() == 0
    assert captured["model_selection"] == {"provider": "local", "model": "auto-id"}

    monkeypatch.setattr(local_models, "discover_models", lambda **kwargs: ["one", "two"])
    with pytest.raises(SystemExit) as error:
        cli.main()
    assert error.value.code == 2


def test_models_and_doctor_commands_do_not_generate(monkeypatch, tmp_path, capsys):
    _isolate_cli(monkeypatch, tmp_path)
    monkeypatch.setattr(cli, "discover_models", lambda **kwargs: ["listed-id"])
    monkeypatch.setattr(cli, "generate", lambda run: pytest.fail("doctor/models never generate"))
    monkeypatch.setattr(sys, "argv", ["video-report", "models"])
    assert cli.main() == 0
    assert "listed-id" in capsys.readouterr().out

    monkeypatch.setattr(
        cli, "run_doctor", lambda: [("model discovery", True, "GET only"), ("ASR", None, "skipped")]
    )
    monkeypatch.setattr(sys, "argv", ["video-report", "doctor"])
    assert cli.main() == 0
    output = capsys.readouterr().out
    assert "[OK] model discovery: GET only" in output
    assert "[SKIP] ASR: skipped" in output


def test_generate_cli_passes_asr_language_auto_through(monkeypatch, tmp_path):
    _isolate_cli(monkeypatch, tmp_path)
    monkeypatch.setenv("PI_PROVIDER", "local")
    monkeypatch.delenv("PI_MODEL", raising=False)
    captured = {}
    monkeypatch.setattr(
        cli,
        "create_run",
        lambda root, url, **kwargs: captured.update(kwargs) or tmp_path / "run",
    )
    monkeypatch.setattr(cli, "generate", lambda run, **kwargs: {"state": "RENDERED"})
    monkeypatch.setattr(
        sys,
        "argv",
        ["video-report", "generate", "https://www.youtube.com/watch?v=jNQXAC9IVRw",
         "--model", "test-model", "--asr-language", "auto"],
    )
    assert cli.main() == 0
    assert captured["asr_language"] == "auto"
