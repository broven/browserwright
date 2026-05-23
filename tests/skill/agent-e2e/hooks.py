"""promptfoo lifecycle hooks: start/stop daemon+Chrome, reset workspace.

Usage in promptfooconfig.yaml:
  extensions: ["file://hooks.py:run_hook"]

Dispatches:
  beforeAll  -> start_session()
  afterAll   -> stop_session()
  beforeEach -> reset_workspace (via workspace.py)

NOTE: promptfoo runs each Python hook call in a separate process via wrapper.py,
so module-level state does NOT persist between calls. We use a JSON state file
to persist PIDs/paths across beforeAll → beforeEach → afterAll.
"""
from __future__ import annotations

import json
import os
import shutil
import signal
import sys
from pathlib import Path

# Ensure the daemon e2e test helpers are importable.
_REPO_ROOT = Path(__file__).resolve().parents[3]
_DAEMON_E2E = _REPO_ROOT / "browserwright-daemon" / "tests" / "e2e"
if str(_DAEMON_E2E) not in sys.path:
    sys.path.insert(0, str(_DAEMON_E2E))

from _patch_extension import patch_extension_dir  # noqa: E402
from _real_browser import (  # noqa: E402
    find_cft_binary,
    kill_chrome,
    launch_cft_with_extension,
    poll_status,
    scrubbed_env,
    spawn_daemon,
)
from workspace import build_workspace, reset_workspace  # noqa: E402

# ---------------------------------------------------------------------------
#  Isolation constants (v2 — distinct from production and v1)
# ---------------------------------------------------------------------------
EXT_PORT = 39989
RDP_PORT = 39990
# Single-global-daemon: isolation is via a dedicated XDG_RUNTIME_DIR (→ a
# distinct fixed socket) + the relay port, NOT a BD_NAME. Kept short for the
# macOS AF_UNIX 104-byte sun_path budget.
RUNTIME_DIR = "/tmp/bd-agent-e2e-rt"

EXT_SOURCE_DIR = _REPO_ROOT / "browserwright-daemon" / "chrome-extension"
WORKSPACE_ROOT = Path(__file__).resolve().parent / "_workspace"
ARTIFACTS_DIR = Path(__file__).resolve().parent / "_artifacts"

# State file persists PIDs across promptfoo's per-call Python processes.
_STATE_FILE = ARTIFACTS_DIR / "_session_state.json"


def _save_state(state: dict) -> None:
    ARTIFACTS_DIR.mkdir(parents=True, exist_ok=True)
    _STATE_FILE.write_text(json.dumps(state), encoding="utf-8")


def _load_state() -> dict | None:
    if _STATE_FILE.exists():
        return json.loads(_STATE_FILE.read_text(encoding="utf-8"))
    return None


def _clear_state() -> None:
    if _STATE_FILE.exists():
        _STATE_FILE.unlink()


def start_session() -> None:
    """Start daemon + Chrome for Testing. Called once per eval run."""
    # Clean up any leftover session from a previous crashed run
    old = _load_state()
    if old:
        _cleanup_from_state(old)

    # Force-kill anything on our ports (in case state file was lost)
    import subprocess as _sp
    import time as _time
    for port in (EXT_PORT, RDP_PORT):
        _sp.run(f"lsof -ti :{port} | xargs kill -9 2>/dev/null",
                shell=True, capture_output=True)
    # Wait for ports to be released (TIME_WAIT)
    from _real_browser import port_free
    for _ in range(60):
        if port_free(EXT_PORT):
            break
        _time.sleep(0.5)

    ARTIFACTS_DIR.mkdir(parents=True, exist_ok=True)
    log_path = ARTIFACTS_DIR / "daemon.log"
    os.makedirs(RUNTIME_DIR, exist_ok=True)

    # 1. Patch extension
    patched_ext = patch_extension_dir(EXT_SOURCE_DIR, relay_port=EXT_PORT)

    # 2. Spawn daemon (isolated by its own XDG_RUNTIME_DIR + relay port)
    daemon = spawn_daemon(
        EXT_PORT,
        RUNTIME_DIR,
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
    chrome = launch_cft_with_extension(cft, patched_ext, rdp_port=RDP_PORT)

    # 4. Wait until extension connects
    poll_status(
        EXT_PORT,
        timeout=15.0,
        proc=daemon.proc,
        log_path=log_path,
        require_extensions=1,
    )

    # 5. Build initial workspace
    import subprocess
    subprocess.run(["rm", "-rf", str(WORKSPACE_ROOT)], check=False)
    build_workspace(WORKSPACE_ROOT)

    # 6. Persist state for other hook calls
    _save_state({
        "daemon_pid": daemon.proc.pid,
        "chrome_pid": chrome.pid,
        "chrome_profile": str(chrome.profile_path),
        "patched_ext": str(patched_ext),
    })


def _cleanup_from_state(state: dict) -> None:
    """Kill processes and clean up paths from saved state."""
    chrome_pid = state.get("chrome_pid")
    if chrome_pid:
        try:
            kill_chrome(chrome_pid)
        except Exception:
            pass

    daemon_pid = state.get("daemon_pid")
    if daemon_pid:
        try:
            os.kill(daemon_pid, signal.SIGTERM)
        except ProcessLookupError:
            pass

    for key in ("chrome_profile", "patched_ext"):
        p = state.get(key)
        if p:
            shutil.rmtree(p, ignore_errors=True)

    _clear_state()


def stop_session() -> None:
    """Tear down Chrome + daemon. Called once at eval end."""
    state = _load_state()
    if state:
        _cleanup_from_state(state)


def run_hook(hook_name: str, context: dict) -> None:
    """promptfoo extensions entry point."""
    if hook_name == "beforeAll":
        start_session()
    elif hook_name == "afterAll":
        stop_session()
    elif hook_name == "beforeEach":
        reset_workspace(WORKSPACE_ROOT)
