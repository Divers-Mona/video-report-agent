"""Subscription-CLI report script with fake CLIs; no real model is called."""

import hashlib
import importlib.util
import json
import sys
import time
from pathlib import Path

import pytest

from video_report_agent.pi import WORKSPACE_SYSTEM_PROMPT, generation_prompt

SCRIPT = Path(__file__).parents[1] / "scripts" / "cli_agent_report.py"
spec = importlib.util.spec_from_file_location("cli_agent_report", SCRIPT)
cli_agent_report = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cli_agent_report)

GOOD_REPORT = (
    "<html><body><h1>t</h1>{{VIDEO_DESCRIPTION}}"
    "<p data-source='unit-000001'>a</p><a href='#unit-000002'>b</a></body></html>"
)
FAKE_CLI = f"""#!{sys.executable}
import json, os, sys, time
mode = os.environ["FAKE_MODE"]
json.dump({{"argv": sys.argv[1:], "stdin": sys.stdin.read(), "cwd": os.getcwd(),
           "has_key": "ANTHROPIC_API_KEY" in os.environ, "pwd": os.environ.get("PWD"),
           "leaked": sorted(k for k in os.environ
                            if k.endswith("_API_KEY")
                            or k in ("OPENAI_BASE_URL", "CLAUDE_CODE_USE_BEDROCK")),
           "python": os.environ.get("VIDEO_REPORT_PYTHON"),
           "utf8": os.environ.get("PYTHONUTF8")}},
          open("../fake-received.json", "w"))
print('{{"type":"result"}}')
reports = {{
    "good": {GOOD_REPORT!r},
    "two_placeholders": (
        "<html><body>{{{{VIDEO_DESCRIPTION}}}}{{{{VIDEO_DESCRIPTION}}}} unit-000001</html>"
    ),
    "unknown_unit": "<html><body>{{{{VIDEO_DESCRIPTION}}}} unit-000001 unit-000999</html>",
    "no_units": "<html><body>{{{{VIDEO_DESCRIPTION}}}} no citations</html>",
    "fragment": "{{{{VIDEO_DESCRIPTION}}}} unit-000001",
}}
if mode in reports:
    open("report.html", "w").write(reports[mode])
if mode == "fail":
    print("quota exhausted", file=sys.stderr)
    sys.exit(3)
if mode == "slow":
    time.sleep(30)
"""


@pytest.fixture
def run_dir(tmp_path):
    run = tmp_path / "runs" / "abc123"
    (run / "download").mkdir(parents=True)
    (run / "transcript.md").write_text(
        "# t\n[unit-000001 | 0.0–1.0s] 一\n[unit-000002 | 1.0–2.0s] 二\n", encoding="utf-8"
    )
    (run / "input.json").write_text(json.dumps({
        "url": "https://example.test/v", "video_id": "BVx", "report_mode": "standard",
        "model_selection": {"api_key": "DO_NOT_STAGE"},
        # Pi-extension features the CLIs cannot provide; the script must stage them off.
        "diagrams": True, "term_correction": True, "visual_enhancement": True,
    }), encoding="utf-8")
    (run / "visual").mkdir()
    (run / "visual" / "observations.jsonl").write_text('{"frame_id":"frame-000001"}\n')
    (run / "download" / "source.info.json").write_text(
        json.dumps({"description": "视频简介", "title": "x"}, ensure_ascii=False),
        encoding="utf-8",
    )
    return run


@pytest.fixture
def fake_cli(tmp_path):
    path = tmp_path / "fake-cli"
    path.write_text(FAKE_CLI, encoding="utf-8")
    path.chmod(0o755)
    return path


def _digest(directory: Path) -> dict:
    return {
        str(path.relative_to(directory)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(directory.rglob("*")) if path.is_file()
    }


def _run(monkeypatch, run_dir, fake_cli, out, mode, *extra):
    monkeypatch.setenv("FAKE_MODE", mode)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "must-not-reach-cli")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "must-not-reach-cli")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://api.example.test")
    monkeypatch.setenv("CLAUDE_CODE_USE_BEDROCK", "1")
    return cli_agent_report.main([
        str(run_dir), "--backend", "claude", "--model", "sonnet", "--effort", "low",
        "--executable", str(fake_cli), "--out", str(out), *extra,
    ])


def test_successful_run_stages_pi_inputs_and_fills_description(
    monkeypatch, run_dir, fake_cli, tmp_path
):
    before = _digest(run_dir)
    out = tmp_path / "out"
    assert _run(monkeypatch, run_dir, fake_cli, out, "good") == 0
    assert _digest(run_dir) == before

    workspace = out / "workspace"
    received = json.loads((out / "fake-received.json").read_text())
    assert Path(received["cwd"]) == workspace
    assert received["stdin"] == generation_prompt(
        workspace, "standard", "report-template.html",
        diagrams=False, term_correction=False, visual_enhancement=False,
    )
    for tool in ("generate_diagram", "lookup_term", "transcript.corrected.md"):
        assert tool not in received["stdin"]
    assert received["has_key"] is False
    assert received["leaked"] == []
    assert received["pwd"] == str(workspace)
    assert received["python"] == sys.executable
    assert received["utf8"] == "1"
    argv = received["argv"]
    assert argv[argv.index("--append-system-prompt") + 1] == WORKSPACE_SYSTEM_PROMPT
    assert argv[argv.index("--model") + 1] == "sonnet"
    assert argv[argv.index("--effort") + 1] == "low"
    assert argv[argv.index("--permission-mode") + 1] == "acceptEdits"

    staged_input = json.loads((workspace / "input.json").read_text())
    assert "model_selection" not in staged_input
    assert staged_input["diagrams"] is False
    assert staged_input["term_correction"] is False
    assert staged_input["visual_enhancement"] is False
    assert not (workspace / "visual").exists()
    for extension in ("report_diagram.ts", "diagram.md", "term_correction.ts"):
        assert not (workspace / extension).exists()
    assert (workspace / "modes" / "standard.md").is_file()
    assert not (workspace / "modes" / "brief.md").exists()
    assert not (workspace / "assets" / "brief-report-template.html").exists()

    report = (workspace / "report.html").read_text(encoding="utf-8")
    assert "{{VIDEO_DESCRIPTION}}" not in report and "视频简介" in report
    manifest = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["status"] == "succeeded" and manifest["exit_code"] == 0
    assert manifest["checks"]["unit_references"] == 2
    assert manifest["checks"]["missing_unit_ids"] == []
    assert "<prompt>" not in manifest["command"]  # Claude receives the prompt on stdin
    assert "must-not-reach-cli" not in (out / "manifest.json").read_text()
    assert (out / "cli.stdout.log").read_text().strip() == '{"type":"result"}'


@pytest.mark.parametrize(
    ("mode", "message"),
    [
        ("none", "report.html was not written"),
        ("two_placeholders", "appears 2 times"),
        ("unknown_unit", "not in transcript.md"),
        ("no_units", "cites no unit IDs"),
        ("fragment", "not a complete HTML document"),
        ("fail", "exited with code 3"),
    ],
)
def test_failed_checks_exit_nonzero_and_keep_logs(
    monkeypatch, run_dir, fake_cli, tmp_path, capsys, mode, message
):
    out = tmp_path / "out"
    assert _run(monkeypatch, run_dir, fake_cli, out, mode) == 1
    manifest = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["status"] == "failed" and message in manifest["error"]
    assert message in capsys.readouterr().err
    assert (out / "workspace" / "transcript.md").is_file()
    if mode == "fail":
        assert "quota exhausted" in (out / "cli.stderr.log").read_text()


def test_timeout_stops_cli_and_fails(monkeypatch, run_dir, fake_cli, tmp_path):
    out = tmp_path / "out"
    assert _run(monkeypatch, run_dir, fake_cli, out, "slow", "--timeout", "1") == 1
    manifest = json.loads((out / "manifest.json").read_text())
    assert manifest["timed_out"] is True and "timed out" in manifest["error"]


def test_codex_and_opencode_commands(tmp_path):
    workspace = tmp_path / "ws"
    command, stdin, env = cli_agent_report.build_command(
        "codex", "codex", model="gpt-5.5", effort="high", workspace=workspace,
        prompt="P", config_dir=tmp_path / "c1",
    )
    assert stdin == "P" and env == {} and command[-1] == "-"
    assert command[command.index("-s") + 1] == "workspace-write"
    assert command[command.index("-C") + 1] == str(workspace)
    assert f"developer_instructions={WORKSPACE_SYSTEM_PROMPT}" in command
    assert "model_reasoning_effort=high" in command

    config = tmp_path / "c2"
    command, stdin, env = cli_agent_report.build_command(
        "opencode", "opencode", model="opencode-go/deepseek-v4.1-flash", effort="max",
        workspace=workspace, prompt="P", config_dir=config,
    )
    assert stdin is None and command[-1] == "P" and "--auto" in command
    assert command[command.index("-m") + 1] == "opencode-go/deepseek-v4.1-flash#max"
    assert env["XDG_CONFIG_HOME"] == str(config)
    settings = json.loads((config / "opencode" / "opencode.json").read_text())
    assert settings["permission"] == {"external_directory": "deny"}
    instructions = Path(settings["instructions"][0])
    assert instructions.read_text(encoding="utf-8").strip() == WORKSPACE_SYSTEM_PROMPT


def test_opencode_variant_given_twice_is_rejected_before_creating_output(run_dir, tmp_path):
    out = tmp_path / "out"
    with pytest.raises(SystemExit) as error:
        cli_agent_report.main([
            str(run_dir), "--backend", "opencode", "--model", "p/m#high", "--effort", "max",
            "--out", str(out),
        ])
    assert error.value.code == 2
    assert not out.exists()


def _manifest_and_argv(monkeypatch, run_dir, fake_cli, tmp_path, backend, *options):
    monkeypatch.setattr(cli_agent_report, "REPO_ROOT", tmp_path / "repo")
    monkeypatch.setenv("FAKE_MODE", "good")
    assert cli_agent_report.main(
        [str(run_dir), "--backend", backend, "--executable", str(fake_cli), *options]
    ) == 0
    (out,) = (tmp_path / "repo" / ".local" / "evals" / "cli-agent-reports" / "abc123").iterdir()
    manifest = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
    return out.name, manifest, json.loads((out / "fake-received.json").read_text())["argv"]


@pytest.mark.parametrize(
    ("backend", "model", "effort", "flags", "slug"),
    [
        ("claude", "sonnet", "medium", [("--model", "sonnet"), ("--effort", "medium")],
         "claude-sonnet-medium-"),
        ("codex", "gpt-6.1-sol", "low",
         [("-m", "gpt-6.1-sol"), ("-c", "model_reasoning_effort=low")],
         "codex-gpt-6.1-sol-low-"),
        ("opencode", "opencode-go/deepseek-v4.1-flash", "max",
         [("-m", "opencode-go/deepseek-v4.1-flash#max")],
         "opencode-opencode-go_deepseek-v4.1-flash-max-"),
    ],
)
def test_backend_defaults(
    monkeypatch, run_dir, fake_cli, tmp_path, backend, model, effort, flags, slug
):
    name, manifest, argv = _manifest_and_argv(monkeypatch, run_dir, fake_cli, tmp_path, backend)
    assert (manifest["model"], manifest["effort"]) == (model, effort)
    assert name.startswith(slug)
    pairs = set(zip(argv, argv[1:]))
    assert all(flag in pairs for flag in flags)


@pytest.mark.parametrize(
    ("backend", "options", "model", "effort", "expected"),
    [
        ("claude", ["--model", "opus", "--effort", "high"], "opus", "high",
         ["--model", "opus", "--effort", "high"]),
        ("codex", ["--model", "gpt-6-luna", "--effort", "medium"], "gpt-6-luna", "medium",
         ["-m", "gpt-6-luna", "-c", "model_reasoning_effort=medium"]),
        ("opencode", ["--model", "opencode-go/glm-5.3", "--effort", "high"],
         "opencode-go/glm-5.3", "high", ["-m", "opencode-go/glm-5.3#high"]),
        # A model#variant already selects the variant; no default variant is appended.
        ("opencode", ["--model", "opencode-go/glm-5.3#low"], "opencode-go/glm-5.3#low", None,
         ["-m", "opencode-go/glm-5.3#low"]),
    ],
)
def test_explicit_selection_overrides_defaults(
    monkeypatch, run_dir, fake_cli, tmp_path, backend, options, model, effort, expected
):
    _, manifest, argv = _manifest_and_argv(
        monkeypatch, run_dir, fake_cli, tmp_path, backend, *options
    )
    assert (manifest["model"], manifest["effort"]) == (model, effort)
    start = argv.index(expected[0])
    assert argv[start:start + len(expected)] == expected


def test_interrupt_stops_cli_and_records_manifest(monkeypatch, run_dir, fake_cli, tmp_path):
    started = []
    real_popen = cli_agent_report.subprocess.Popen

    class InterruptedPopen(real_popen):
        def communicate(self, *args, **kwargs):
            started.append(self)
            time.sleep(0.5)
            raise KeyboardInterrupt

    monkeypatch.setattr(cli_agent_report.subprocess, "Popen", InterruptedPopen)
    out = tmp_path / "out"
    with pytest.raises(KeyboardInterrupt):
        _run(monkeypatch, run_dir, fake_cli, out, "slow")
    assert started[0].returncode is not None  # the 30 s fake CLI was killed
    manifest = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["status"] == "interrupted"
    assert manifest["error"] == "interrupted: KeyboardInterrupt"
    assert manifest["report"] is None
