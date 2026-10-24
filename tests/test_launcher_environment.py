import os
import shlex
import subprocess
import sys
from pathlib import Path


def _write_executable(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")
    path.chmod(0o755)


def test_run_wsl_child_can_find_and_start_project_yt_dlp(tmp_path):
    project = tmp_path / "project"
    external = tmp_path / "external"
    venv_bin = project / ".venv-wsl" / "bin"
    launcher = project / "scripts" / "run-wsl.sh"
    for directory in (
        external,
        launcher.parent,
        venv_bin,
        project / ".tooling" / "wsl" / "node" / "bin",
        project / ".tooling" / "wsl" / "pi" / "node_modules" / ".bin",
        project / ".tooling" / "wsl" / "bin",
    ):
        directory.mkdir(parents=True, exist_ok=True)
    source = Path(__file__).parents[1] / "scripts" / "run-wsl.sh"
    launcher.write_bytes(source.read_bytes())

    python_code = "\n".join(
        (
            "import os, shutil, subprocess, sys",
            "executable = shutil.which('yt-dlp')",
            "print(executable or 'MISSING', flush=True)",
            "print(os.getenv('PI_PROVIDER', '') + '|' + os.getenv('PI_BASE_URL', ''), flush=True)",
            "if not executable:",
            "    raise SystemExit(3)",
            "result = subprocess.run([executable, '--version'], check=False)",
            "raise SystemExit(result.returncode)",
        )
    )
    cli_stub = (
        "#!/usr/bin/env bash\nexec "
        + shlex.quote(sys.executable)
        + " -c "
        + shlex.quote(python_code)
        + "\n"
    )
    _write_executable(venv_bin / "video-report", cli_stub)
    yt_dlp_stub = venv_bin / "yt-dlp"
    _write_executable(yt_dlp_stub, "#!/usr/bin/env bash\nprintf 'mock-yt-dlp-2026.08.19\\n'\n")

    environment = os.environ.copy()
    environment.update(
        PATH="/usr/bin:/bin",
        PI_PROVIDER="local",
        PI_BASE_URL="http://127.0.0.1:43219/v1",
    )
    result = subprocess.run(
        ["/bin/bash", str(launcher), "doctor"],
        cwd=external,
        env=environment,
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert str(yt_dlp_stub) in result.stdout.splitlines()
    assert "mock-yt-dlp-2026.08.19" in result.stdout.splitlines()
    assert "local|http://127.0.0.1:43219/v1" in result.stdout.splitlines()
