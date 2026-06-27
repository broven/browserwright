"""GH #18 regression case — end-to-end through a REAL daemon, no Chrome needed.

Bug (as reported, browserwright 0.6.14): on an `--backend=extension` session
with no extension connected to the daemon relay, the FIRST browser command
failed with::

    ExecutorUnavailable: ensureExecutor failed for session '<id>':
    CDP BrowserwrightDaemon.ensureExecutor failed:
    ws closed: no close frame received or sent

Two compounding defects produced that misleading symptom:

  1. ``cdp._open_unix_websocket`` left the connect-phase ``settimeout`` on the
     socket, so it leaked into steady state as a per-recv read timeout. Any RPC
     reply slower than ``connect_timeout`` (~8s) tore the transport down as
     ``no close frame received or sent`` — the confusing ``ws closed``.
  2. ``ensureExecutor`` on the extension backend blocked on the full ~60s
     extension-open grace (``wait_ready``), far past the client's CDP reply
     deadline (~30s), so even after (1) the real cause never reached the agent.

Fixes: reset the socket timeout after connect; and make the executor control
plane fail FAST with an actionable message when no extension is connected.

This case stands up a fully isolated extension daemon (own socket + ephemeral
relay/facade ports, no real browser) and drives the actual
``BrowserwrightDaemon.ensureExecutor`` RPC through the real :class:`CDPSession`.
It asserts the agent now gets a fast, actionable error instead of ``ws closed``.

Run:
    uv run pytest tests/daemon/test_repro_gh18_extension_ws_closed.py -v
"""
from __future__ import annotations

import contextlib
import json
import socket
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

import pytest

from browserwright.cdp import CDPSession
from browserwright.errors import CDPError


def _free_port() -> int:
    """Grab an ephemeral TCP port (small TOCTOU race is fine for a test)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]
    finally:
        s.close()


def _seed_extension_session(bs_home: Path) -> str:
    """Write a persistent extension session into the isolated ledger so the
    daemon's ``?session=<id>`` dispatch resolves it (an unknown session id is
    refused at connect with ws close 1008, which we must not conflate with the
    bug under test)."""
    sessions_dir = bs_home / "sessions"
    sessions_dir.mkdir(parents=True, exist_ok=True)
    ledger = sessions_dir / "ledger.json"
    sid = f"gh18-{uuid.uuid4().hex[:8]}"
    now = time.time()
    record = {
        "id": sid, "backend": "extension", "workspace": None, "owner": "attach",
        "name": "gh18-repro", "created_at": now, "last_seen": now,
    }
    try:
        existing = json.loads(ledger.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        existing = {"next_id": 1, "sessions": {}}
    existing.setdefault("sessions", {})[sid] = record
    ledger.write_text(json.dumps(existing), encoding="utf-8")
    return sid


@pytest.fixture
def extension_daemon_no_extension():
    """An isolated extension-backend daemon with NO extension connected.

    Fully isolated from any developer/global daemon: a private
    ``XDG_RUNTIME_DIR`` (→ its own unix socket) plus ephemeral relay + facade
    ports. Yields ``(sock_path, bs_home)``."""
    runtime_dir = tempfile.mkdtemp(prefix="bwg18")  # short base → AF_UNIX path fits
    bs_home = Path(runtime_dir) / "bs_home"
    bs_home.mkdir(parents=True, exist_ok=True)
    relay_port, facade_port = _free_port(), _free_port()

    env = {
        **_clean_env(),
        "XDG_RUNTIME_DIR": runtime_dir,
        "TMPDIR": runtime_dir,
        "BS_HOME": str(bs_home),
        "BD_CONFIG": "",  # ignore the developer's toml (ports/backend/relay_url)
        "BD_EXTENSION_PORT": str(relay_port),
        # Shrink the no-extension fast-fail wait so this fast-gate test doesn't
        # sit for the default 10s grace (we assert behavior, not the budget).
        "BW_EXT_READY_BUDGET_S": "0.3",
    }
    # Same interpreter for daemon + client → identical version, so the client's
    # version-skew auto-restart never fires and contaminates the run.
    proc = subprocess.Popen(
        [sys.executable, "-m", "browserwright.daemon.cli", "serve",
         "--backend", "extension",
         "--extension-port", str(relay_port),
         "--facade-port", str(facade_port)],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env,
    )
    sock_path = Path(runtime_dir) / "browserwright-daemon.sock"
    try:
        deadline = time.monotonic() + 15.0
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                pytest.fail(f"daemon exited early (code={proc.returncode})")
            if sock_path.exists():
                break
            time.sleep(0.05)
        else:
            pytest.fail("isolated daemon never bound its socket")
        yield str(sock_path), bs_home
    finally:
        proc.terminate()
        with contextlib.suppress(subprocess.TimeoutExpired):
            proc.wait(timeout=5)
        if proc.poll() is None:
            proc.kill()


def _clean_env() -> dict:
    import os
    # Drop inherited browserwright overrides that could leak ports/sockets in.
    drop = {"BD_EXTENSION_PORT", "BD_FACADE_PORT", "BD_BACKEND", "BD_SESSION"}
    return {k: v for k, v in os.environ.items() if k not in drop}


def test_ensure_executor_no_extension_is_fast_and_actionable(
        extension_daemon_no_extension):
    """The reporter's exact RPC, against a real daemon with no extension."""
    sock_path, bs_home = extension_daemon_no_extension
    sid = _seed_extension_session(bs_home)

    sess = CDPSession(f"ws+unix://{sock_path}?session={sid}&client=gh18")
    t0 = time.monotonic()
    with pytest.raises(CDPError) as exc:
        sess.send("BrowserwrightDaemon.ensureExecutor", bsSession=sid)
    elapsed = time.monotonic() - t0

    msg = str(exc.value)
    # 1. The confusing symptom is GONE — the transport is no longer torn down
    #    by a leaked connect timeout.
    assert "no close frame" not in msg, msg
    assert "ws closed" not in msg, msg
    # 2. The error is ACTIONABLE — it names the real cause + the fix.
    assert "no browserwright extension is connected" in msg, msg
    assert "chrome://extensions" in msg, msg
    # 3. It returns WITHIN the client CDP reply deadline (~30s) — it neither
    #    drops at ~8s nor hangs to the ~60s extension grace.
    assert elapsed < 25.0, f"ensureExecutor took {elapsed:.1f}s (expected fast-fail)"
