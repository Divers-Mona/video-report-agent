import hashlib
import json
import os
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest
import yt_dlp_ejs
from yt_dlp_ejs.yt import solver

from video_report_agent import doctor

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"


def doctor_rows(monkeypatch):
    monkeypatch.setattr(doctor, "_playwright_check", lambda: (True, "browser"))
    monkeypatch.setattr(doctor, "_model_check", lambda: (None, "skipped"))
    monkeypatch.setattr(doctor, "_asr_check", lambda: (None, "skipped"))
    monkeypatch.setattr(doctor, "_asr_decoder_check", lambda: (None, "skipped"))
    return {name: (ok, detail) for name, ok, detail in doctor.run_doctor()}


@pytest.mark.parametrize(
    "available,version,expected",
    [
        (True, "deno 2.3.0 (stable)", True),
        (True, "deno 2.9.8 (stable)", True),
        (True, "deno 2.2.9 (stable)", False),
        (True, "unrecognized", False),
        (False, "not found on PATH", False),
    ],
)
def test_doctor_reports_supported_deno(monkeypatch, available, version, expected):
    monkeypatch.setattr(
        doctor,
        "_tool_check",
        lambda name, *args: (available, version) if name == "deno" else (True, "tool"),
    )
    ok, detail = doctor_rows(monkeypatch)["YouTube Deno runtime"]
    assert ok is expected
    if not expected:
        assert "scripts/install-deno.sh" in detail


@pytest.mark.parametrize("problem", [None, "missing", "version", "core", "lib", "metadata"])
def test_doctor_checks_local_ejs_version_and_both_bundles(monkeypatch, problem):
    monkeypatch.setattr(doctor, "_tool_check", lambda *args: (True, "deno 2.9.8 (stable)"))
    monkeypatch.setattr(
        doctor, "package_requires", lambda _: ['yt-dlp-ejs==0.8.0; extra == "default"']
    )
    monkeypatch.setattr(yt_dlp_ejs, "version", "0.8.0")
    monkeypatch.setattr(solver, "core", lambda: "local core")
    monkeypatch.setattr(solver, "lib", lambda: "local library")
    if problem == "missing":
        monkeypatch.setitem(sys.modules, "yt_dlp_ejs", None)
    elif problem == "version":
        monkeypatch.setattr(yt_dlp_ejs, "version", "0.7.0")
    elif problem in {"core", "lib"}:
        monkeypatch.setattr(solver, problem, lambda: " ")
    elif problem == "metadata":
        monkeypatch.setattr(doctor, "package_requires", lambda _: [])
    ok, detail = doctor_rows(monkeypatch)["YouTube EJS scripts"]
    assert ok is (problem is None)
    assert "sync the locked project dependencies" in detail if problem else "0.8.0" in detail


def executable(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    path.chmod(0o755)


@pytest.fixture
def installer(tmp_path):
    if sys.platform != "linux":
        pytest.skip("WSL/Linux shell installer")
    project = tmp_path / "project with spaces"
    (project / "scripts").mkdir(parents=True)
    script = project / "scripts/install-deno.sh"
    shutil.copyfile(SCRIPTS / script.name, script)
    python = project / ".venv-wsl/bin/python"
    python.parent.mkdir(parents=True)
    python.symlink_to(sys.executable)
    release = tmp_path / "release"
    release.mkdir()
    tools = tmp_path / "mock-bin"
    tools.mkdir()
    log = tmp_path / "curl.jsonl"
    executable(tools / "uname", '#!/bin/sh\nprintf "%s\\n" "$TEST_ARCH"\n')
    executable(
        tools / "curl",
        f"""#!{sys.executable}
import json, os, pathlib, shutil, sys
args=sys.argv[1:]
url=next(arg for arg in args if arg.startswith('https://'))
assert url.startswith('https://github.com/denoland/deno/releases/download/v2.9.8/')
with open(os.environ['TEST_CURL_LOG'], 'a') as log:
    log.write(json.dumps(url)+'\\n')
shutil.copyfile(pathlib.Path(os.environ['TEST_RELEASE']) / url.rsplit('/',1)[1],
                args[args.index('-o')+1])
""",
    )
    env = dict(
        os.environ,
        PATH=f"{tools}:{os.environ['PATH']}",
        TEST_ARCH="x86_64",
        TEST_RELEASE=str(release),
        TEST_CURL_LOG=str(log),
    )

    def prepare(arch="x86_64", version="2.9.8", checksum="valid"):
        env["TEST_ARCH"] = arch
        archive = release / f"deno-{arch}-unknown-linux-gnu.zip"
        with zipfile.ZipFile(archive, "w") as package:
            package.writestr("deno", f"#!/bin/sh\nprintf 'deno {version} (stable)\\n'\n")
            package.writestr("../outside", "must never be extracted")
        digest = hashlib.sha256(archive.read_bytes()).hexdigest()
        filename = archive.name
        if checksum == "mismatch":
            digest = "0" * 64
        elif checksum == "filename":
            filename = "../../outside"
        elif checksum == "malformed":
            digest = "invalid"
        archive.with_suffix(".zip.sha256sum").write_text(f"{digest}  {filename}\n")

    def run():
        return subprocess.run(
            ["bash", str(script)], env=env, capture_output=True, text=True, timeout=15, check=False
        )

    return project, prepare, run, log


@pytest.mark.parametrize("arch", ["x86_64", "aarch64"])
def test_deno_installer_verifies_and_installs_only_binary_then_skips(installer, arch):
    project, prepare, run, log = installer
    prepare(arch)
    result = run()
    assert result.returncode == 0, result.stderr
    assert "SHA-256 verified" in result.stdout
    deno = project / ".tooling/wsl/bin/deno"
    assert subprocess.check_output([str(deno), "--version"], text=True).startswith("deno 2.9.8 ")
    requests = log.read_text().splitlines()
    assert len(requests) == 2
    assert all(arch in json.loads(line) for line in requests)
    assert not (project / ".tooling/wsl/outside").exists()
    assert not list((project / ".tooling/wsl").glob(".deno-install.*"))
    assert run().returncode == 0
    assert log.read_text().splitlines() == requests


@pytest.mark.parametrize("checksum", ["mismatch", "filename", "malformed"])
def test_deno_installer_rejects_invalid_checksum_and_preserves_existing_binary(installer, checksum):
    project, prepare, run, _ = installer
    deno = project / ".tooling/wsl/bin/deno"
    old = "#!/bin/sh\nprintf 'deno 2.2.0 (stable)\\n'\n"
    executable(deno, old)
    prepare(checksum=checksum)
    result = run()
    assert result.returncode != 0
    assert deno.read_text() == old
    assert not list((project / ".tooling/wsl").glob(".deno-install.*"))


@pytest.mark.parametrize("installed", ["2.2.0", "2.9.80"])
def test_deno_installer_replaces_other_versions(installer, installed):
    project, prepare, run, _ = installer
    deno = project / ".tooling/wsl/bin/deno"
    executable(deno, f"#!/bin/sh\nprintf 'deno {installed} (stable)\\n'\n")
    prepare()
    result = run()
    assert result.returncode == 0, result.stderr
    assert subprocess.check_output([str(deno), "--version"], text=True).startswith("deno 2.9.8 ")


def test_deno_installer_checks_downloaded_binary_version_before_replacement(installer):
    project, prepare, run, _ = installer
    prepare(version="2.2.0")
    result = run()
    assert result.returncode != 0
    assert "unexpected version" in result.stderr
    assert not (project / ".tooling/wsl/bin/deno").exists()


def test_wsl_launcher_exposes_project_deno_and_disables_update_check(tmp_path):
    if sys.platform != "linux":
        pytest.skip("WSL/Linux shell launcher")
    project = tmp_path / "project"
    (project / "scripts").mkdir(parents=True)
    script = project / "scripts/run-wsl.sh"
    shutil.copyfile(SCRIPTS / script.name, script)
    executable(project / ".tooling/wsl/bin/deno", "#!/bin/sh\nexit 0\n")
    executable(
        project / ".venv-wsl/bin/video-report",
        '#!/bin/sh\ncommand -v deno\nprintf "%s\\n" "$DENO_NO_UPDATE_CHECK"\n',
    )
    result = subprocess.run(
        ["bash", str(script), "doctor"], capture_output=True, text=True, timeout=10, check=False
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == [str(project / ".tooling/wsl/bin/deno"), "1"]
