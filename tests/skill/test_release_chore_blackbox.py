"""`scripts/release-chore.sh` end to end, with every outside party faked.

The chore is the last step of every release, runs on the maintainer's Mac
under `/bin/bash` (3.2), and had no test: v0.18.6 went out, then the chore
died on `upgrade_args[@]: unbound variable` -- bash 3.2 treats an empty array
as unset under `set -u` -- so the global install was never updated.

The fakes: a scratch repo whose HEAD already carries the release tag (the
chore's resume path, so no version bump), a local bare repo as `origin`, a
`file://` PyPI and npm responses, and `browserwright-daemon` / `mise` stubs on PATH.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
BASH = "/bin/bash"

pytestmark = pytest.mark.skipif(
    sys.platform != "darwin" or not Path(BASH).exists(),
    reason="exercises the macOS system bash the chore actually runs under")


def _stub(path: Path, body: str) -> None:
    path.write_text("#!/bin/sh\n" + body)
    path.chmod(0o755)


def _run_chore(tmp_path: Path, *args: str,
               npm_versions: tuple[str, ...] = ("9.9.9",),
               ) -> tuple[subprocess.CompletedProcess, str]:
    work = tmp_path / "work"
    (work / "scripts").mkdir(parents=True)
    shutil.copy(REPO / "scripts" / "release-chore.sh", work / "scripts")
    git = ["git", "-c", "user.name=t", "-c", "user.email=t@t"]
    subprocess.run(["git", "init", "-q", "--bare", str(tmp_path / "origin.git")],
                   check=True)
    for cmd in (["init", "-q"], ["add", "."], ["commit", "-qm", "init"],
                ["tag", "-a", "v9.9.9", "-m", "v9.9.9"],
                ["remote", "add", "origin", str(tmp_path / "origin.git")]):
        subprocess.run(git + cmd, cwd=work, check=True)

    pypi = tmp_path / "pypi.json"
    pypi.write_text(json.dumps({"releases": {"9.9.9": [{}]}}))
    npm = tmp_path / "npm.json"
    npm.write_text(json.dumps({
        "dist-tags": {"latest": npm_versions[-1] if npm_versions else "9.9.8"},
        "versions": {v: {} for v in npm_versions}}))
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    mise_log = tmp_path / "mise.log"
    _stub(bin_dir / "browserwright-daemon", "exit 0\n")  # `activity`: idle
    _stub(bin_dir / "mise", f'echo "$@" >> "{mise_log}"\n')
    env = {
        **os.environ,
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        "BROWSERWRIGHT_PYPI_URL": pypi.as_uri(),
        "BROWSERWRIGHT_PYPI_POLL_INTERVAL": "0",
        "BROWSERWRIGHT_NPM_URL": npm.as_uri(),
        "BROWSERWRIGHT_NPM_POLL_INTERVAL": "0",
        "BROWSERWRIGHT_NPM_WAIT_TIMEOUT": "1",
    }
    proc = subprocess.run([BASH, "scripts/release-chore.sh", *args], cwd=work,
                          env=env, capture_output=True, text=True, timeout=60)
    return proc, mise_log.read_text() if mise_log.exists() else ""


def test_unforced_chore_reaches_the_global_upgrade(tmp_path):
    proc, mise_calls = _run_chore(tmp_path, "false")
    assert proc.returncode == 0, proc.stderr
    assert mise_calls.splitlines() == ["run upgrade-global"]


def test_forced_chore_passes_force_through(tmp_path):
    proc, mise_calls = _run_chore(tmp_path, "true")
    assert proc.returncode == 0, proc.stderr
    assert mise_calls.splitlines() == ["run upgrade-global --force"]


def test_chore_waits_for_the_pi_package_on_npm(tmp_path):
    """v0.19.1: PyPI had the release, npm did not yet, and the upgrade's pi
    step failed. Without the npm release the chore must not start it."""
    proc, mise_calls = _run_chore(tmp_path, "false", npm_versions=("9.9.8",))
    assert proc.returncode != 0
    assert "Timed out waiting for @browserwright/pi 9.9.9 on npm" in proc.stderr
    assert mise_calls == ""
