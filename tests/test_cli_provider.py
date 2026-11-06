"""--provider/--thinking reach PiRunner; protocol fakes only, no real model."""

import asyncio
import json
import sys

import pytest

import video_report_agent.cli as cli
import video_report_agent.local_models as local_models
from video_report_agent.pi import (
    PI_THINKING_LEVELS,
    WORKSPACE_SYSTEM_PROMPT,
    PiRunner,
    generation_prompt,
)

URL = "https://www.bilibili.com/video/BV1cZ8x6sEhF/"


@pytest.fixture
def captured(monkeypatch, tmp_path):
    monkeypatch.setattr(cli, "PROJECT_ROOT", tmp_path)
    (tmp_path / ".env").write_text("", encoding="utf-8")
    monkeypatch.setenv("PI_PROVIDER", "deepseek")
    monkeypatch.setenv("PI_MODEL", "env-model")
    monkeypatch.setattr(
        local_models, "discover_models", lambda **kwargs: pytest.fail("no discovery expected")
    )
    result = {}
    monkeypatch.setattr(
        cli, "create_run",
        lambda root, url, **kwargs: result.update(kwargs) or tmp_path / "run",
    )
    monkeypatch.setattr(cli, "generate", lambda run, **kwargs: {"state": "RENDERED"})
    return result


def _main(monkeypatch, *args):
    monkeypatch.setattr(sys, "argv", ["video-report", "generate", URL, *args])
    return cli.main()


def test_provider_model_and_thinking_are_saved(monkeypatch, captured):
    assert _main(monkeypatch, "--provider", "zhipu", "--model", "glm-x", "--thinking", "high") == 0
    assert captured["model_selection"] == {
        "provider": "zhipu", "model": "glm-x", "thinking": "high"
    }


def test_non_local_provider_requires_model(monkeypatch, captured, capsys):
    with pytest.raises(SystemExit) as error:
        _main(monkeypatch, "--provider", "zhipu")
    assert error.value.code == 2
    assert "--provider zhipu requires --model" in capsys.readouterr().err
    assert captured == {}


def test_thinking_choices_follow_pi(monkeypatch, captured):
    assert PI_THINKING_LEVELS == ("off", "minimal", "low", "medium", "high", "xhigh", "max")
    with pytest.raises(SystemExit) as error:
        _main(monkeypatch, "--thinking", "extreme")
    assert error.value.code == 2


def test_without_new_options_selection_is_unchanged(monkeypatch, captured):
    assert _main(monkeypatch) == 0
    assert "model_selection" not in captured or captured["model_selection"] is None
    assert _main(monkeypatch, "--model", "chosen") == 0
    assert captured["model_selection"] == {"provider": "deepseek", "model": "chosen"}


def test_thinking_alone_keeps_environment_provider(monkeypatch, captured):
    assert _main(monkeypatch, "--thinking", "max") == 0
    assert captured["model_selection"] == {"thinking": "max"}


def test_local_provider_override_discovers_and_ignores_env_model(monkeypatch, captured):
    monkeypatch.setattr(local_models, "discover_models", lambda **kwargs: ["only-local"])
    assert _main(monkeypatch, "--provider", "local") == 0
    assert captured["model_selection"] == {"provider": "local", "model": "only-local"}


def test_selection_reaches_pi_command(monkeypatch, tmp_path):
    executable = tmp_path / "fake-pi"
    executable.write_text(
        f"""#!{sys.executable}
import json,sys
open("received-argv.json", "w").write(json.dumps(sys.argv[1:]))
json.loads(sys.stdin.readline())
open("report.html", "w").write("<html><body></body></html>")
print(json.dumps({{"type":"message_end","message":{{"role":"assistant","stopReason":"stop"}}}}))
print(json.dumps({{"type":"agent_settled"}}),flush=True)
"""
    )
    executable.chmod(0o755)
    monkeypatch.setattr("video_report_agent.pi.shutil.which", lambda _: str(executable))
    monkeypatch.setenv("PI_CODING_AGENT_DIR", str(tmp_path / "pi"))
    workspace = tmp_path / "run"
    workspace.mkdir()
    (workspace / "transcript.md").write_text("sample")
    (workspace / "input.json").write_text(json.dumps({"report_mode": "brief"}))

    selection = {"provider": "zhipu", "model": "glm-x", "thinking": "xhigh"}
    asyncio.run(PiRunner(**selection, isolation="local").run(workspace))

    received = json.loads((workspace / "received-argv.json").read_text())
    assert received[received.index("--provider") + 1] == "zhipu"
    assert received[received.index("--model") + 1] == "glm-x"
    assert received[received.index("--thinking") + 1] == "xhigh"
    # The CLI-agent script shares these texts with the Pi path.
    assert received[received.index("--append-system-prompt") + 1] == WORKSPACE_SYSTEM_PROMPT
    invocation = json.loads((workspace / "invocation.json").read_text())
    # input.json has no feature keys: Pi defaults diagrams on, the rest off.
    assert invocation["prompt"] == generation_prompt(
        workspace, "brief", "brief-report-template.html",
        term_correction=False, visual_enhancement=False, diagrams=True,
    )
