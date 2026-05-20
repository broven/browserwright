"""promptfoo lifecycle hooks: start/stop daemon+Chrome, reset workspace.

Usage in promptfooconfig.yaml:
  extensions: ["file://hooks.py:run_hook"]

Dispatches:
  beforeAll  -> start_session()
  afterAll   -> stop_session()
  beforeEach -> reset_workspace (via workspace.py)
"""
from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

# Ensure the daemon e2e test helpers are importable.
_REPO_ROOT = Path(__file__).resolve().parents[3]
_DAEMON_E2E = _REPO_ROOT / "browser-daemon" / "tests" / "e2e"
if str(_DAEMON_E2E) not in sys.path:
    sys.path.insert(0, str(_DAEMON_E2E))

from _patch_extension import patch_extension_dir  # noqa: E402
from _real_browser import (  # noqa: E402
    ChromeHandle,
    DaemonHandle,
    find_cft_binary,
    kill_chrome,
    launch_cft_with_extension,
    poll_status,
    scrubbed_env,
    spawn_daemon,
    stop_daemon,
)
from workspace import build_workspace, reset_workspace  # noqa: E402

# ---------------------------------------------------------------------------
#  Isolation constants (v2 — distinct from production and v1)
# ---------------------------------------------------------------------------
EXT_PORT = 39989
RDP_PORT = 39990
DAEMON_NAME = "bd-agent-e2e"

EXT_SOURCE_DIR = _REPO_ROOT / "browser-daemon" / "chrome-extension"
WORKSPACE_ROOT = Path(__file__).resolve().parent / "_workspace"
ARTIFACTS_DIR = Path(__file__).resolve().parent / "_artifacts"

# ---------------------------------------------------------------------------
#  Module-level state (session singleton)
# ---------------------------------------------------------------------------
_daemon: DaemonHandle | None = None
_chrome: ChromeHandle | None = None
_patched_ext: Path | None = None


def start_session() -> None:
    """Start daemon + Chrome for Testing. Called once per eval run."""
    global _daemon, _chrome, _patched_ext

    ARTIFACTS_DIR.mkdir(parents=True, exist_ok=True)
    log_path = ARTIFACTS_DIR / "daemon.log"

    # 1. Patch extension
    _patched_ext = patch_extension_dir(EXT_SOURCE_DIR, relay_port=EXT_PORT)

    # 2. Spawn daemon
    _daemon = spawn_daemon(
        EXT_PORT,
        DAEMON_NAME,
        log_path,
        env=scrubbed_env(),
    )

    # 3. Find and launch Chrome for Testing
    cft = find_cft_binary()
    if cft is None:
        raise RuntimeError(
            "Chrome for Testing not found. Install via: "
            "npx @puppeteer/browsers install chrome@stable "
            "--path /tmp/chrome-for-testing"
        )
    _chrome = launch_cft_with_extension(cft, _patched_ext, rdp_port=RDP_PORT)

    # 4. Wait until extension connects
    poll_status(
        EXT_PORT,
        timeout=15.0,
        proc=_daemon.proc,
        log_path=log_path,
        require_extensions=1,
    )

    # 5. Build initial workspace (reset if leftover from previous run)
    reset_workspace(WORKSPACE_ROOT)


def stop_session() -> None:
    """Tear down Chrome + daemon. Called once at eval end."""
    global _daemon, _chrome, _patched_ext

    if _chrome is not None:
        kill_chrome(_chrome.pid)
        shutil.rmtree(_chrome.profile_path, ignore_errors=True)
        _chrome = None

    if _daemon is not None:
        stop_daemon(_daemon)
        _daemon = None

    if _patched_ext is not None:
        shutil.rmtree(_patched_ext, ignore_errors=True)
        _patched_ext = None


def run_hook(hook_name: str, context: dict) -> None:
    """promptfoo extensions entry point."""
    if hook_name == "beforeAll":
        start_session()
    elif hook_name == "afterAll":
        stop_session()
    elif hook_name == "beforeEach":
        reset_workspace(WORKSPACE_ROOT)
