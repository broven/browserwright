"""pytest configuration for real-Chrome E2E tests.

These tests:
- launch a real Chrome with the patched extension (port 29989)
- spawn a real `browser-daemon serve`
- drive everything through the `browser-skill` CLI

They are SKIPPED unless explicitly selected, either by path
(`pytest tests/e2e/`) or by marker (`pytest -m real_chrome`).
The patcher unit test (test_patch_extension.py) does NOT carry the marker
so it remains discoverable in the inner loop.
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path

import pytest

# Test-only ports. Chosen to be distinct from production (19989) and far enough
# from common dev ports to reduce collisions.
TEST_EXT_PORT = 29989
TEST_RDP_PORT = 29990
TEST_NAME = "bd-e2e"


def pytest_collection_modifyitems(config, items):
    """Auto-mark every test in tests/e2e/ (except _patch_extension test) as
    `real_chrome`, and skip them unless the user asked for them.

    Selection rule:
        - User passed `-m real_chrome` (or any expression that matches)  -> run
        - User passed an explicit path under tests/e2e/ matching the test -> run
        - Else -> skip with a clear reason.
    """
    rootdir = config.rootpath
    # 1. Tag everything in tests/e2e/ (except the patcher unit test) with
    #    `real_chrome`.
    for item in items:
        try:
            rel = item.path.relative_to(rootdir)
        except ValueError:
            continue
        parts = rel.parts
        if len(parts) >= 2 and parts[0] == "tests" and parts[1] == "e2e":
            if item.path.name == "test_patch_extension.py":
                continue
            item.add_marker(pytest.mark.real_chrome)

    # 2. If the user did NOT explicitly opt-in, skip real_chrome tests.
    if _opted_in_to_real_chrome(config):
        return
    skip = pytest.mark.skip(
        reason="real_chrome E2E -- opt in with `pytest tests/e2e/` "
               "or `pytest -m real_chrome`"
    )
    for item in items:
        if "real_chrome" in item.keywords:
            item.add_marker(skip)


def _opted_in_to_real_chrome(config) -> bool:
    # Marker expression mentions real_chrome.
    mark_expr = config.getoption("-m", default="") or ""
    if "real_chrome" in mark_expr:
        return True
    # Any positional arg points under tests/e2e/.
    for arg in config.args:
        if "tests/e2e" in arg.replace("\\", "/"):
            return True
    return False


# ---------------------------------------------------------------------------
#   Fixtures
# ---------------------------------------------------------------------------


@dataclass
class DaemonHandle:
    proc: subprocess.Popen
    ext_port: int
    name: str
    log_path: Path


def _port_free(port: int) -> bool:
    with closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as s:
        try:
            s.bind(("127.0.0.1", port))
            return True
        except OSError:
            return False


@pytest.fixture(scope="session")
def e2e_artifacts_dir() -> Path:
    d = Path(__file__).parent / "_artifacts"
    d.mkdir(parents=True, exist_ok=True)
    return d


@pytest.fixture(scope="session")
def e2e_daemon(e2e_artifacts_dir, tmp_path_factory):
    """Spawn `browser-daemon serve --backend extension --extension-port N
    --name bd-e2e` for the duration of the session. Yields a DaemonHandle.
    """
    if not _port_free(TEST_EXT_PORT):
        pytest.fail(
            f"port {TEST_EXT_PORT} already in use; another test daemon? "
            "Use `lsof -i :29989` to find it."
        )

    log_path = e2e_artifacts_dir / "daemon.log"
    log_fh = open(log_path, "wb")  # noqa: SIM115 — closed in teardown

    env = os.environ.copy()
    env["BD_NAME"] = TEST_NAME
    # Force config to a tmp path so we don't write to ~/.config/browser-daemon
    env["BS_DAEMON_CONFIG_PATH"] = str(tmp_path_factory.mktemp("bd-cfg"))

    proc = subprocess.Popen(
        [
            sys.executable, "-m", "browser_daemon.cli",
            "serve",
            "--backend", "extension",
            "--extension-port", str(TEST_EXT_PORT),
            "--name", TEST_NAME,
            "-v",
        ],
        stdout=log_fh,
        stderr=subprocess.STDOUT,
        env=env,
    )

    # Wait until /__status__ responds.
    deadline = time.monotonic() + 10.0
    last_err: Exception | None = None
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            log_fh.flush()
            pytest.fail(
                f"daemon exited early with code {proc.returncode}; "
                f"see {log_path}"
            )
        try:
            with urllib.request.urlopen(
                f"http://127.0.0.1:{TEST_EXT_PORT}/__status__", timeout=0.5
            ) as resp:
                if resp.status == 200:
                    break
        except (urllib.error.URLError, OSError) as e:
            last_err = e
            time.sleep(0.2)
    else:
        log_fh.flush()
        pytest.fail(
            f"daemon /__status__ never came up within 10s; last err={last_err}; "
            f"see {log_path}"
        )

    yield DaemonHandle(proc=proc, ext_port=TEST_EXT_PORT, name=TEST_NAME, log_path=log_path)

    # Teardown.
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=2)
    log_fh.close()
