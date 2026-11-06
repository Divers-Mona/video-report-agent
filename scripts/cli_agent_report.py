"""Generate a report from a transcribed run with a logged-in coding-agent CLI.

Local evaluation/generation only: this bypasses Pi, so there is no agent_settled
completion check and no pi.events.jsonl. Inputs, the staged Skill, the prompt and
the workspace system prompt are produced by the same functions the Pi path uses.

    uv run python scripts/cli_agent_report.py <run-dir|run-id> --backend claude \
        --model sonnet --effort low
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

from video_report_agent.pi import (
    SKILL,
    WORKSPACE_SYSTEM_PROMPT,
    generation_prompt,
    stage_report_skill,
)
from video_report_agent.report_content import (
    VIDEO_DESCRIPTION_PLACEHOLDER,
    fill_video_description,
)
from video_report_agent.task_container import stage_inputs

REPO_ROOT = Path(__file__).resolve().parents[1]
BACKENDS = ("claude", "codex", "opencode")
UNIT_ID = re.compile(r"unit-\d{6}")
# Pi's tool set is read,write,edit,bash; the CLIs get the equivalent built-ins.
CLAUDE_TOOLS = "Read,Write,Edit,Bash"
# The children use their own logged-in state. Keys, endpoints and provider
# switches inherited from this shell would move Claude/Codex off the
# subscription (and every *_API_KEY is removed so no key reaches the agent);
# the Claude Code markers would make a nested `claude` treat itself as a
# sub-session.
SCRUBBED_ENV = (
    "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "OPENAI_API_KEY", "CODEX_API_KEY",
    "PI_API_KEY", "CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT", "CLAUDE_CODE_SSE_PORT",
    "CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX", "ANTHROPIC_BASE_URL",
    "OPENAI_BASE_URL",
)
# Used when --model/--effort are omitted (user-chosen defaults).
DEFAULTS = {
    "claude": ("sonnet", "medium"),
    "codex": ("gpt-6.1-sol", "low"),
    "opencode": ("opencode-go/deepseek-v4.1-flash", "max"),
}


def child_environment(workspace: Path, extra: dict[str, str]) -> dict[str, str]:
    env = {
        key: value for key, value in os.environ.items()
        if not key.endswith("_API_KEY") and key not in SCRUBBED_ENV
    }
    # OpenCode takes its project directory from PWD, which a parent shell may have
    # set to the repository root; keep it equal to the real working directory.
    env.update(extra, VIDEO_REPORT_PYTHON=sys.executable, PWD=str(workspace))
    return env


def resolve_selection(backend: str, model: str | None, effort: str | None):
    """Apply per-backend defaults; an OpenCode model#variant already sets the effort."""
    default_model, default_effort = DEFAULTS[backend]
    model = model or default_model
    if effort is None and not (backend == "opencode" and "#" in model):
        effort = default_effort
    return model, effort


class ReportCheckError(RuntimeError):
    pass


def resolve_run(value: str) -> Path:
    candidate = Path(value)
    if not candidate.is_dir():
        candidate = REPO_ROOT / "runs" / value
    if not (candidate / "transcript.md").is_file():
        raise SystemExit(f"error: {value} is not a run directory with transcript.md")
    return candidate.resolve()


def find_executable(name: str) -> str | None:
    # On Windows, Python 3.12 may return npm's extensionless sh shim first.
    if os.name == "nt" and not Path(name).suffix:
        for extension in os.environ.get("PATHEXT", ".EXE;.CMD;.BAT").split(os.pathsep):
            if extension and (found := shutil.which(name + extension)):
                return found
    return shutil.which(name)


def slug(value: str | None) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", value or "default").strip("_") or "default"


def build_command(
    backend: str, executable: str, *, model: str | None, effort: str | None,
    workspace: Path, prompt: str, config_dir: Path,
) -> tuple[list[str], str | None, dict[str, str]]:
    """Return (argv, stdin text, extra environment) for one headless CLI run."""
    if backend == "claude":
        command = [
            executable, "-p",
            # Skip CLAUDE.md, skills, plugins, hooks and MCP while keeping login state.
            "--safe-mode",
            # Confine file tools to the working directory and drop unnamed code tools.
            "--restricted",
            "--tools", CLAUDE_TOOLS,
            "--permission-mode", "acceptEdits",
            "--allowedTools", "Bash",
            "--append-system-prompt", WORKSPACE_SYSTEM_PROMPT,
            "--output-format", "stream-json", "--verbose",
            "--no-session-persistence",
        ]
        if model:
            command += ["--model", model]
        if effort:
            command += ["--effort", effort]
        return command, prompt, {}
    if backend == "codex":
        # Values that fail TOML parsing are taken literally by `codex -c`.
        command = [
            executable, "exec", "--skip-git-repo-check", "--ephemeral",
            "--ignore-user-config",
            "-s", "workspace-write", "-C", str(workspace), "--json",
            "-c", f"developer_instructions={WORKSPACE_SYSTEM_PROMPT}",
            # Do not load AGENTS.md from the enclosing repository.
            "-c", "project_doc_max_bytes=0",
        ]
        if model:
            command += ["-m", model]
        if effort:
            command += ["-c", f"model_reasoning_effort={effort}"]
        return command + ["-"], prompt, {}
    # OpenCode has no system-prompt flag: a private config directory supplies the
    # same text as an instruction file and replaces the user's global config.
    opencode_config = config_dir / "opencode"
    opencode_config.mkdir(parents=True)
    instructions = opencode_config / "system-prompt.md"
    instructions.write_text(WORKSPACE_SYSTEM_PROMPT + "\n", encoding="utf-8")
    (opencode_config / "opencode.json").write_text(json.dumps({
        "$schema": "https://opencode.ai/config.json",
        "instructions": [instructions.as_posix()],
        "permission": {"external_directory": "deny"},
        "autoupdate": False,
    }, indent=2), encoding="utf-8")
    command = [
        executable, "run", "--standalone", "--format", "json", "--auto",
        "-m", f"{model}#{effort}" if effort else model, prompt,
    ]
    env = {
        "XDG_CONFIG_HOME": str(config_dir),
        "OPENCODE_DISABLE_PROJECT_CONFIG": "1",
        "OPENCODE_DISABLE_CLAUDE_CODE": "1",
    }
    return command, None, env


def check_report(report: Path, transcript: Path) -> dict:
    """Raise ReportCheckError with the collected checks when the report is unusable."""
    checks: dict = {"report_exists": report.is_file() and not report.is_symlink()}
    if not checks["report_exists"]:
        raise ReportCheckError("report.html was not written", checks)
    html = report.read_text(encoding="utf-8")
    lowered = html.lower()
    # Same completeness rule as PiRunner.
    checks["complete_html"] = all(tag in lowered for tag in ("<html", "</html>", "<body"))
    checks["video_description_placeholders"] = html.count(VIDEO_DESCRIPTION_PLACEHOLDER)
    references = UNIT_ID.findall(html)
    known = set(UNIT_ID.findall(transcript.read_text(encoding="utf-8")))
    missing = sorted(set(references) - known)
    checks.update(
        unit_references=len(references), unique_unit_references=len(set(references)),
        missing_unit_ids=missing,
    )
    problems = [] if checks["complete_html"] else ["report.html is not a complete HTML document"]
    if checks["video_description_placeholders"] != 1:
        problems.append(
            f"{{{{VIDEO_DESCRIPTION}}}} appears {checks['video_description_placeholders']} "
            "times (expected 1)"
        )
    if not references:
        problems.append("report cites no unit IDs")
    if missing:
        problems.append(f"{len(missing)} cited unit IDs are not in transcript.md: {missing[:10]}")
    if problems:
        raise ReportCheckError("; ".join(problems), checks)
    return checks


def run_cli(command, stdin_text, env, cwd, out: Path, timeout: float) -> tuple[int | None, bool]:
    """Run the CLI with raw output logged; return (exit code, timed_out)."""
    with (out / "cli.stdout.log").open("wb") as stdout, \
            (out / "cli.stderr.log").open("wb") as stderr:
        process = subprocess.Popen(
            command, cwd=cwd, env=env, stdout=stdout, stderr=stderr,
            stdin=subprocess.PIPE if stdin_text is not None else subprocess.DEVNULL,
            start_new_session=os.name != "nt",
        )
        try:
            process.communicate(
                stdin_text.encode("utf-8") if stdin_text is not None else None, timeout=timeout
            )
        except subprocess.TimeoutExpired:
            stop_tree(process)
            return process.returncode, True
        except BaseException:
            # Ctrl-C and other interruptions must not leave the agent running.
            stop_tree(process)
            raise
    return process.returncode, False


def stop_tree(process: subprocess.Popen) -> None:
    # npm shims and agent CLIs spawn children; stop the whole tree.
    if os.name == "nt":
        if process.poll() is None:
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(process.pid)],
                           capture_output=True, check=False)
    else:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    process.wait()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("run", help="run directory or run ID under runs/")
    parser.add_argument("--backend", choices=BACKENDS, required=True)
    defaults = "; ".join(f"{name} {model} (effort {effort})" for name, (model, effort) in DEFAULTS.items())
    parser.add_argument(
        "--model",
        help=f"CLI model name; OpenCode uses provider/model[#variant]. Defaults: {defaults}",
    )
    parser.add_argument(
        "--effort",
        help="Claude --effort, Codex model_reasoning_effort, or OpenCode model variant "
        "(default per backend; omitted when the OpenCode model already has #variant)",
    )
    parser.add_argument("--out", type=Path, help="new output directory")
    parser.add_argument("--timeout", type=float, default=1800, help="seconds (default 1800)")
    parser.add_argument("--executable", help="CLI executable (default: backend name on PATH)")
    args = parser.parse_args(argv)

    if args.backend == "opencode" and args.effort and args.model and "#" in args.model:
        parser.error("give the OpenCode variant either as model#variant or --effort")
    args.model, args.effort = resolve_selection(args.backend, args.model, args.effort)
    run_dir = resolve_run(args.run)
    metadata = json.loads((run_dir / "input.json").read_text(encoding="utf-8"))
    report_mode = metadata.get("report_mode", "standard")
    if report_mode not in ("standard", "brief"):
        parser.error("report_mode must be standard or brief")
    executable = find_executable(args.executable or args.backend)
    if executable is None:
        parser.error(f"{args.executable or args.backend} is not on PATH")
    started = datetime.now().astimezone()
    out = (args.out or REPO_ROOT / ".local" / "evals" / "cli-agent-reports" / run_dir.name / (
        f"{args.backend}-{slug(args.model)}-{slug(args.effort)}-"
        f"{started.strftime('%Y%m%d-%H%M%S')}"
    )).resolve()
    out.mkdir(parents=True)
    workspace = stage_inputs(run_dir, out / "workspace")
    template_name = stage_report_skill(SKILL.resolve(), workspace, report_mode)
    prompt = generation_prompt(workspace, report_mode, template_name)
    command, stdin_text, extra_env = build_command(
        args.backend, executable, model=args.model, effort=args.effort,
        workspace=workspace, prompt=prompt, config_dir=out / "cli-config",
    )
    env = child_environment(workspace, extra_env)

    print(f"Running {args.backend} in {workspace}", flush=True)
    clock = time.monotonic()
    report = workspace / "report.html"
    checks: dict = {}
    exit_code, timed_out, error = None, False, None

    def write_manifest(status: str) -> None:
        manifest = {
            "status": status,
            "error": error,
            "backend": args.backend,
            "model": args.model,
            "effort": args.effort,
            "run": str(run_dir),
            "report_mode": report_mode,
            "template": template_name,
            # The prompt is sent on stdin (claude, codex) or as the last argument (opencode).
            "command": [("<prompt>" if part == prompt else part) for part in command],
            "prompt": prompt,
            "cwd": str(workspace),
            "env_overrides": sorted(extra_env),
            "started_at": started.isoformat(timespec="seconds"),
            "finished_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "elapsed_seconds": round(time.monotonic() - clock, 1),
            "exit_code": exit_code,
            "timed_out": timed_out,
            "checks": checks,
            "report": str(report) if status == "succeeded" else None,
        }
        (out / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )

    try:
        exit_code, timed_out = run_cli(command, stdin_text, env, workspace, out, args.timeout)
    except OSError as exc:
        error = f"could not start {executable}: {exc}"
    except BaseException as exc:
        # run_cli has already stopped the CLI process tree.
        error = f"interrupted: {type(exc).__name__}"
        write_manifest("interrupted")
        print(f"{error}; logs and workspace kept in {out}", file=sys.stderr)
        raise
    elapsed = round(time.monotonic() - clock, 1)
    if error:
        pass
    elif timed_out:
        error = f"{args.backend} timed out after {args.timeout:g}s"
    elif exit_code != 0:
        error = f"{args.backend} exited with code {exit_code}"
    else:
        try:
            checks = check_report(report, workspace / "transcript.md")
            fill_video_description(report, run_dir)
            checks["video_description_filled"] = True
        except ReportCheckError as exc:
            error, checks = exc.args
        except (OSError, ValueError) as exc:
            error = f"video description fill failed: {exc}"
    write_manifest("failed" if error else "succeeded")
    if error:
        print(f"error: {error}; logs and workspace kept in {out}", file=sys.stderr)
        for log in ("cli.stderr.log", "cli.stdout.log"):
            tail = (out / log).read_bytes()[-2000:].decode("utf-8", "replace").strip()
            if tail:
                print(f"--- tail of {log}\n{tail}", file=sys.stderr)
                break
        return 1
    print(f"Report: {report} ({elapsed}s, {checks['unit_references']} unit references)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
