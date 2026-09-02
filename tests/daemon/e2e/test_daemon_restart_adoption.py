"""A planned daemon replacement adopts resident executors intact.

This is deliberately a real extension-backend test.  The executor must lose
its Playwright facade connection, survive the daemon's normal SIGTERM shutdown,
and reconnect through the replacement daemon before its persistent Python
``state`` can be observed again.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path

from browserwright.daemon import _ipc

from .conftest import (
    TEST_EXT_FACADE_PORT,
    TEST_EXT_PORT,
    endpoint_url,
    scrubbed_env,
)
from .helpers import run_skill


def _environment(runtime_dir: str) -> dict[str, str]:
    env = scrubbed_env()
    env["PATH"] = str(Path(sys.executable).parent) + os.pathsep + env.get("PATH", "")
    env["XDG_RUNTIME_DIR"] = runtime_dir
    env["TMPDIR"] = runtime_dir
    env["BW_DAEMON_URL"] = endpoint_url(TEST_EXT_FACADE_PORT)
    env["BD_EXTENSION_PORT"] = str(TEST_EXT_PORT)
    env["BD_CONFIG"] = ""
    env["BS_HOME"] = str(
        Path(__file__).resolve().parent / "_bs_home" / "extension"
    )
    env["NO_PROXY"] = "127.0.0.1,localhost"
    env["no_proxy"] = "127.0.0.1,localhost"
    return env


def _seed_sessions(env: dict[str, str], sessions: dict[str, str]) -> None:
    ledger = Path(env["BS_HOME"]) / "sessions" / "ledger.json"
    ledger.parent.mkdir(parents=True, exist_ok=True)
    data = json.loads(ledger.read_text()) if ledger.exists() else {
        "next_id": 1,
        "sessions": {},
    }
    now = time.time()
    for sid, name in sessions.items():
        data["sessions"][sid] = {
            "id": sid,
            "backend": "extension",
            "workspace": None,
            "owner": "attach",
            "name": name,
            "created_at": now,
            "last_seen": now,
        }
    ledger.write_text(json.dumps(data), encoding="utf-8")


def _status(env: dict[str, str], *, timeout: float = 30.0,
            extensions: int = 0) -> dict:
    deadline = time.monotonic() + timeout
    last_error: BaseException | None = None
    while time.monotonic() < deadline:
        try:
            result = subprocess.run(
                ["browserwright-daemon", "ps", "--json", "--timeout", "3"],
                capture_output=True,
                text=True,
                timeout=3,
                env=env,
            )
            payload = json.loads(result.stdout)
            relay = payload.get("relay") or {}
            if len(relay.get("extensions") or []) >= extensions:
                return payload
        except (OSError, subprocess.SubprocessError, ValueError) as exc:
            last_error = exc
        time.sleep(0.2)
    raise AssertionError(
        f"replacement daemon did not become ready within {timeout}s: {last_error}"
    )


def _executor_rows(status: dict, session_ids: set[str]) -> dict[str, dict]:
    rows = {
        str(row.get("session_id")): row
        for row in status.get("executors") or []
        if str(row.get("session_id")) in session_ids
    }
    assert rows.keys() == session_ids, status.get("executors")
    return rows


def _run_state(env: dict[str, str], sid: str, code: str):
    result = run_skill(
        code,
        backend="extension",
        runtime_dir=env["XDG_RUNTIME_DIR"],
        extra_env={"BD_SESSION": sid},
        timeout=90,
    )
    assert result.returncode == 0, (
        f"session {sid} failed\nstdout={result.stdout}\nstderr={result.stderr}"
    )
    return result


def _stop(proc: subprocess.Popen) -> None:
    if proc.poll() is not None:
        return
    proc.terminate()
    try:
        proc.wait(timeout=8)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=3)


def test_sigterm_replacement_adopts_both_executors_with_state(
    ext_ready, e2e_daemon, request, monkeypatch,
):
    """The handoff marker preserves identity and state for every session."""
    runtime_dir = e2e_daemon.runtime_dir
    env = _environment(runtime_dir)
    session_ids = {
        f"restart-a-{uuid.uuid4().hex}",
        f"restart-b-{uuid.uuid4().hex}",
    }
    sid_a, sid_b = sorted(session_ids)
    _seed_sessions(env, {sid_a: "restart-adoption-a", sid_b: "restart-adoption-b"})

    replacement: subprocess.Popen | None = None
    try:
        _run_state(env, sid_a, 'state["token"] = "alpha"; print(state["token"])')
        _run_state(env, sid_b, 'state["token"] = "beta"; print(state["token"])')

        before = _executor_rows(_status(env, extensions=1), session_ids)
        identities = {
            sid: (row["pid"], row["executor_id"])
            for sid, row in before.items()
        }
        assert identities[sid_a] != identities[sid_b]

        # The marker is scoped by runtime dir and fingerprinted to this exact
        # daemon pid.  SIGTERM remains the ordinary graceful-shutdown path.
        monkeypatch.setenv("XDG_RUNTIME_DIR", runtime_dir)
        assert _ipc.request_executor_handoff(e2e_daemon.proc.pid)
        e2e_daemon.proc.terminate()
        e2e_daemon.proc.wait(timeout=8)

        log = Path(runtime_dir) / "replacement-stdout.log"
        with log.open("ab") as output:
            replacement = subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "browserwright.daemon.cli",
                    "serve",
                    "--backend",
                    "extension",
                    "--extension-port",
                    str(TEST_EXT_PORT),
                    "--facade-port",
                    str(TEST_EXT_FACADE_PORT),
                    "-v",
                ],
                stdout=output,
                stderr=subprocess.STDOUT,
                env=env,
            )
        e2e_daemon.proc = replacement
        request.session.addfinalizer(lambda: _stop(replacement))

        after = _executor_rows(_status(env, timeout=60, extensions=1), session_ids)
        for sid in session_ids:
            assert (after[sid]["pid"], after[sid]["executor_id"]) == identities[sid]
            assert after[sid]["adopted"] is True

        # Exercise A first, then B: reconnecting/using one adopted executor must
        # neither replace nor mutate the other session's executor or namespace.
        a = _run_state(env, sid_a, 'print(state["token"]); state["seen"] = True')
        assert "alpha" in a.stdout
        middle = _executor_rows(_status(env), session_ids)
        assert (middle[sid_b]["pid"], middle[sid_b]["executor_id"]) == identities[sid_b]

        b = _run_state(env, sid_b, 'print(state["token"]); print(state.get("seen"))')
        assert "beta" in b.stdout
        assert "None" in b.stdout
    finally:
        # End both attach-owned sessions through the replacement daemon so the
        # shared session-scoped e2e fixture is left with no resident executors.
        active_daemon = replacement or e2e_daemon.proc
        if active_daemon.poll() is None:
            for sid in session_ids:
                subprocess.run(
                    ["browserwright", "session", "end", "--session", sid],
                    capture_output=True,
                    text=True,
                    timeout=30,
                    env=env,
                )
