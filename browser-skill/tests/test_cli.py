"""End-to-end CLI tests via subprocess (verifies the entrypoint actually wires up)."""
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest


def _bin() -> str:
    # Prefer a console-script that's actually on PATH; fall back to invoking
    # the CLI as ``python -m browser_skill.cli`` so tests are robust against
    # the venv's bin/ not being on PATH.
    found = shutil.which("browser-skill")
    if found:
        return found
    return None


def _run(args, *, env=None, input_text=None, cwd=None):
    full_env = os.environ.copy()
    if env:
        full_env.update(env)
    binpath = _bin()
    if binpath is not None:
        cmd = [binpath, *args]
    else:
        cmd = [sys.executable, "-m", "browser_skill.cli", *args]
    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        env=full_env,
        input=input_text,
        cwd=cwd,
        timeout=15,
    )


def test_version():
    r = _run(["version"])
    assert r.returncode == 0
    assert r.stdout.strip()


def test_help():
    r = _run(["--help"])
    assert r.returncode == 0
    assert "browser-skill" in r.stdout


def test_doctor_runs_without_daemon(tmp_path):
    r = _run(
        ["doctor", "--json"],
        env={
            "BS_HOME": str(tmp_path),
            # Point at a binary that doesn't exist so we hit the synthetic path.
            "PATH": "/usr/bin:/bin",
        },
    )
    assert r.returncode == 0
    info = json.loads(r.stdout)
    assert info["schema_version"] == 1
    assert "skill_version" in info


def test_list_tasks_smoke(tmp_path):
    r = _run(
        ["list-tasks", "--json=true"],
        env={"BS_HOME": str(tmp_path)},
        cwd=str(tmp_path),
    )
    assert r.returncode == 0
    # Just make sure the JSON parses — bundled site-skills are inside the
    # checkout so depending on cwd we may or may not see them.
    json.loads(r.stdout)


def test_memory_show_global(tmp_path):
    r = _run(
        ["memory", "show", "--global=true"],
        env={"BS_HOME": str(tmp_path)},
    )
    assert r.returncode == 0
    data = json.loads(r.stdout)
    assert "frontmatter" in data
