"""L1 -- Playwright `connect_over_cdp` against the daemon's CDP facade (cdp).

Phase A1 acceptance (task 05-24-tab-handle-model-for-code-agents, PR1):
a real Playwright client connects to the daemon's NEW Playwright-facing CDP
facade ws endpoint, opens a page, navigates, and reads the title — proving
`chromium.connect_over_cdp(daemon_ws)` drives the cdp backend end to end.

The facade is an additive TCP ws+HTTP endpoint (`/json/version` discovery + a
transparent CDP passthrough) layered beside the agent unix socket; it does not
touch the existing `BrowserwrightDaemon.*` client path.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from .conftest import (  # noqa: F401
    TEST_FACADE_L1_PORT as TEST_FACADE_PORT,
    TEST_CDP_PORT,
    _isolated_runtime_dir,
    _kill_chrome,
)


@pytest.fixture
def e2e_cdp_facade_daemon(e2e_chrome_cdp, e2e_artifacts_dir):
    """Spawn the single daemon with `--facade-port N` against the cdp Chrome,
    isolated via a throwaway XDG_RUNTIME_DIR. Yields the facade
    port once the facade's `/json/version` answers."""
    import shutil

    log_path = e2e_artifacts_dir / "daemon-cdp-facade.log"
    log_fh = open(log_path, "wb")  # noqa: SIM115 — closed in teardown

    runtime_dir = _isolated_runtime_dir()
    env = os.environ.copy()
    env["XDG_RUNTIME_DIR"] = runtime_dir
    env["TMPDIR"] = runtime_dir
    env["BD_CDP_PORT"] = str(TEST_CDP_PORT)
    env["BS_HOME"] = str(Path(__file__).resolve().parent / "_bs_home" / "cdp")
    env["BD_CONFIG"] = ""

    subprocess.run(["browserwright-daemon", "stop"], capture_output=True, env=env)

    proc = subprocess.Popen(
        [
            sys.executable, "-m", "browserwright.daemon.cli", "serve",
            "--backend", "cdp",
            "--facade-port", str(TEST_FACADE_PORT),
            "-v",
        ],
        stdout=log_fh,
        stderr=subprocess.STDOUT,
        env=env,
    )

    # Wait until the facade's /json/version answers (the daemon binds it during
    # run_serve startup).
    # NOTE: on ANY setup failure we must reap ``proc`` before raising — the
    # teardown section only runs after ``yield`` (leak guard, mirrors
    # conftest.e2e_rdp_daemon).
    def _fail(msg: str):
        log_fh.flush()
        try:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=2)
        except Exception:
            pass
        pytest.fail(msg)

    version_url = f"http://127.0.0.1:{TEST_FACADE_PORT}/json/version"
    deadline = time.monotonic() + 10.0
    last_err = None
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            _fail(f"cdp facade daemon exited early ({proc.returncode}); "
                  f"see {log_path}")
        try:
            with urllib.request.urlopen(version_url, timeout=0.5) as resp:
                if resp.status == 200:
                    break
        except (urllib.error.URLError, OSError) as e:
            last_err = e
            time.sleep(0.2)
    else:
        _fail(f"facade /json/version never came up; last={last_err}; "
              f"see {log_path}")

    yield TEST_FACADE_PORT

    subprocess.run(["browserwright-daemon", "stop"], capture_output=True, env=env)
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=2)
    log_fh.close()
    shutil.rmtree(runtime_dir, ignore_errors=True)


def test_facade_json_version_advertises_ws(e2e_cdp_facade_daemon):
    """The CDP discovery route returns a valid bootstrap payload with a
    reachable webSocketDebuggerUrl pointing back at the facade."""
    port = e2e_cdp_facade_daemon
    with urllib.request.urlopen(
        f"http://127.0.0.1:{port}/json/version", timeout=2
    ) as resp:
        body = json.loads(resp.read().decode("utf-8"))
    assert body["Protocol-Version"] == "1.3"
    ws = body["webSocketDebuggerUrl"]
    assert ws == f"ws://127.0.0.1:{port}/cdp"


def test_connect_over_cdp_drives_cdp_backend(e2e_cdp_facade_daemon):
    """ACCEPTANCE: `chromium.connect_over_cdp(facade_ws)` connects, opens a
    page, navigates, and reads the title — proving the facade drives the cdp
    Chrome with a real Playwright client."""
    playwright_api = pytest.importorskip("playwright.sync_api")
    sync_playwright = playwright_api.sync_playwright

    port = e2e_cdp_facade_daemon
    facade_ws = f"ws://127.0.0.1:{port}/cdp"

    with sync_playwright() as p:
        browser = p.chromium.connect_over_cdp(facade_ws)
        try:
            # context.pages() should enumerate the cdp Chrome's existing tab(s).
            context = (browser.contexts[0] if browser.contexts
                       else browser.new_context())
            page = context.pages[0] if context.pages else context.new_page()
            page.goto("data:text/html,<title>facade-ok</title><h1>hi</h1>",
                      wait_until="load")
            assert page.title() == "facade-ok"
            # A simple evaluate proves the page domain is fully wired.
            assert page.evaluate("1 + 1") == 2
        finally:
            browser.close()
