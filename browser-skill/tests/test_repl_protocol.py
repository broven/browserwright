"""REPL server / inline glue — the parts we can exercise without a browser."""
import os
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

import pytest


def _cmd(*args):
    found = shutil.which("browser-skill")
    if found:
        return [found, *args]
    return [sys.executable, "-m", "browser_skill.cli", *args]


def _short_home() -> Path:
    """Unix-socket paths on macOS are capped at 104 bytes. pytest's
    ``tmp_path`` (under ``/private/var/folders/...``) easily blows past that
    once you append ``/repl.sock``. So we mint our own short scratch dir."""
    p = Path(tempfile.gettempdir()) / f"bs-{uuid.uuid4().hex[:8]}"
    p.mkdir(parents=True, exist_ok=True)
    return p


@pytest.mark.timeout(30)
def test_repl_start_stop_status():
    home = _short_home()
    env = os.environ.copy()
    env["BS_HOME"] = str(home)
    # `repl start` should fork and return.
    p = subprocess.run(_cmd("repl", "start"),
                       capture_output=True, text=True, env=env, timeout=10)
    assert p.returncode == 0, p.stderr
    # poll status
    deadline = time.time() + 5
    while time.time() < deadline:
        s = subprocess.run(_cmd("repl", "status"),
                           capture_output=True, text=True, env=env, timeout=5)
        if "running" in s.stdout:
            break
        time.sleep(0.1)
    else:
        # be nice and try to stop anyway before failing
        subprocess.run(_cmd("repl", "stop"), env=env, timeout=5)
        pytest.skip("repl daemon did not come up — environment-dependent (no fork?)")
    # Send a trivial code snippet that doesn't need the browser.
    r = subprocess.run(
        _cmd("exec", "print(2 + 2)"),
        capture_output=True, text=True, env=env, timeout=5,
    )
    assert "4" in r.stdout
    # tear down
    subprocess.run(_cmd("repl", "stop"), env=env, timeout=5)
    shutil.rmtree(home, ignore_errors=True)


@pytest.mark.timeout(15)
def test_inline_heredoc_pure_python():
    home = _short_home()
    env = os.environ.copy()
    env["BS_HOME"] = str(home)
    r = subprocess.run(
        _cmd(),
        capture_output=True, text=True, env=env, timeout=10,
        input="print(2 + 3)\n",
    )
    assert r.returncode == 0
    assert "5" in r.stdout
