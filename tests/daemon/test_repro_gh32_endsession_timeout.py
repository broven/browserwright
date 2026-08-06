"""Issue #32 regression tests: `endSession` initiate-then-join contract.

Before the fix, `endSession` was a synchronous verb over an operation whose
worst case outlived every caller's timeout: the caller raised `TimeoutError`
while the daemon still held the per-session lock and still ran teardown, then
the daemon completed the teardown the caller never saw and installed its
terminal tombstone — while Layer 2 kept the ledger row, i.e. a ledger entry
whose ordinary operations are refused.

After the fix the daemon returns at the initiate boundary (bounded fast phase:
clients revoked, executor reaped, phase=terminating) and the unbounded
workspace teardown continues as a daemon-side task; a retried `endSession`
joins it and returns the FINAL result, so the caller can never time out
mid-teardown.

These tests use the REAL client (`_rpc.call`), the REAL verb handler
(`Router._handle_end_session`), and the REAL per-session lifecycle lock
(`ExecutorRegistry`) over an in-process unix-socket ws — the fakes are limited
to the upstream adapter (slow tab close) and a daemon shim carrying only
`executors`.
"""
from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs

import pytest
import websockets

from browserwright.daemon import _ipc, _rpc
from browserwright.daemon.config import Config
from browserwright.daemon.server.proxy import Router
from browserwright.daemon.server.state import DaemonState, UpstreamPhase

#: Relative mismatch only: the real CLI end-session timeout is 10.0s and the
#: daemon teardown budget is 8.0s + reap (worst case > 10s). Scaling both down
#: keeps the test fast while preserving the *relationship* the issue is about:
#: caller timeout < daemon lock-hold duration.
CLIENT_TIMEOUT_S = 0.25
SLOW_TEARDOWN_S = 0.8


class SlowExtensionUpstream:
    """Adapter whose teardown outlives the old caller timeout — simulates a
    cold/stale extension: serial tab closes, each entering a reconnect
    window."""

    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.done = asyncio.Event()

    async def end_session_before(
        self, session_id: str, group_id: int | None = None, *, deadline: float,
    ) -> dict:
        self.started.set()
        try:
            await asyncio.sleep(SLOW_TEARDOWN_S)
        finally:
            self.done.set()
        return {
            "ok": True, "closed": [1, 2, 3],
            "failed": [], "unknown": [], "kept": [],
        }


async def _serve_one(router, state, conn) -> None:
    """Mirror of ``listener.Adapter.serve_one`` for the in-process socket:
    parse ``?session=``, allocate + register the client, route frames."""
    query = parse_qs((conn.request.path or "").split("?", 1)[-1])
    session_id = (query.get("session") or [None])[0]
    label = (query.get("client") or ["repro"])[0]
    client = state.allocate_client(
        label, session_id=session_id, session_name="repro")
    client.connection_token = object()

    async def send_to_client(text: str) -> None:
        await conn.send(text)

    router.register_client(client.client_id, send_to_client)
    try:
        async for raw in conn:
            text = raw if isinstance(raw, str) else raw.decode("utf-8", "replace")
            await router.route_from_client(client, text)
    except websockets.exceptions.ConnectionClosed:
        pass  # caller gave up; the in-flight verb keeps running (real behavior)
    finally:
        await router.release_client(client.client_id)
        router.unregister_client(client.client_id)


@pytest.mark.asyncio
async def test_gh32_slow_teardown_never_outlives_the_caller(
    tmp_path, monkeypatch,
):
    """The fix: on a slow teardown the caller receives the initiate response
    promptly (no timeout), the queued ensure is refused from initiate time,
    and a retried endSession JOINS and returns the final result."""
    from browserwright import session_registry
    from browserwright.daemon.server.executor_registry import ExecutorRegistry

    # Isolate socket + executor discovery files from the real daemon. Like
    # ``_ipc._runtime_dir``, use /tmp: AF_UNIX sun_path has a hard 104-byte
    # budget on macOS and pytest's tmp_path (under /private/var/folders) blows
    # it.
    runtime = Path(tempfile.mkdtemp(prefix="bw-gh32-", dir="/tmp"))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(runtime))
    sock = _ipc.sock_path()
    monkeypatch.setattr(
        session_registry, "get",
        lambda sid: {"id": sid, "backend": "extension", "name": "repro"},
    )

    state = DaemonState(backend_name="extension")
    state.upstream_phase = UpstreamPhase.CONNECTED
    router = Router(state)
    upstream = SlowExtensionUpstream()
    router.upstream = upstream
    registry = ExecutorRegistry()
    router.daemon = SimpleNamespace(executors=registry)

    async def serve():
        async with websockets.unix_serve(
            lambda conn: _serve_one(router, state, conn), str(sock),
        ):
            await asyncio.Future()  # pragma: no cover - never returns

    server_task = asyncio.create_task(serve())
    await asyncio.sleep(0.05)  # let the socket bind
    try:
        # 1. Initiate: even with a client timeout far below the teardown
        #    duration, the caller gets a prompt answer — the mismatch is gone.
        initiate = await _rpc.call(
            Config(), "BrowserwrightDaemon.endSession",
            {"session": "s1"},
            client_label="cli-end-session", timeout=CLIENT_TIMEOUT_S,
            browser_session="s1",
        )
        assert initiate["ok"] is True
        assert initiate["initiated"] is True
        assert initiate["phase"] == "terminating"
        assert upstream.started.is_set()
        assert not upstream.done.is_set(), \
            "initiate must return before the workspace teardown finishes"

        # 2. The ordinary operation a user would retry next — ensureExecutor —
        #    is refused from initiate time (pending marker, no lock waiting).
        with pytest.raises(RuntimeError, match="is terminating"):
            await registry.ensure_with_preflight("s1", _noop)

        # 3. The daemon finishes the teardown the initiate promised, and
        #    installs the terminal tombstone.
        await asyncio.wait_for(upstream.done.wait(), timeout=5.0)
        await asyncio.sleep(0)  # let the pending task publish its result
        assert registry._terminal_results["s1"]["ok"] is True

        # 4. A retried endSession JOINS and returns the FINAL result — the
        #    caller learns the real outcome instead of guessing.
        joined = await _rpc.call(
            Config(), "BrowserwrightDaemon.endSession",
            {"session": "s1"},
            client_label="cli-end-session", timeout=5.0,
            browser_session="s1",
        )
        assert joined["ok"] is True
        assert joined.get("initiated") is not True
        assert joined["closed"] == [1, 2, 3]

        # 5. The session is terminal: ensure is now refused outright.
        with pytest.raises(RuntimeError, match="has ended"):
            await registry.ensure_with_preflight("s1", _noop)
    finally:
        server_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await server_task


@pytest.mark.asyncio
async def test_gh32_failed_end_call_keeps_ledger_row_although_daemon_went_terminal(
    tmp_path, monkeypatch,
):
    """Layer-2 consequence (unchanged by the fix, locked as a reminder): a
    `session end` that cannot confirm completion keeps the ledger row — while
    the daemon underneath may already be terminal, so ordinary operations on
    that row are refused. The fix makes the confirm path reliable (initiate +
    join), so this only remains reachable via a genuinely unreachable daemon."""
    from browserwright import session_create, session_registry as reg
    from browserwright.errors import DaemonUnavailable

    monkeypatch.setenv("BS_HOME", str(tmp_path))
    # Avoid the auto-start path: the daemon "is running" but its CLI exits 3
    # (main() maps the client-side TimeoutError to exit code 3 — see
    # `daemon/cli.py` main(): `except Exception` → 3).
    monkeypatch.setattr(session_create, "_daemon_is_running", lambda: True)
    monkeypatch.setattr(session_create, "_run", lambda cmd, **kwargs: 3)

    sid = reg.allocate(backend="extension", owner="attach", name="repro")
    record = reg.get(sid)

    with pytest.raises(DaemonUnavailable, match="kept for retry"):
        session_create.end(record)

    # The row survives the failed call — from Layer 2's perspective the
    # daemon never confirmed anything (exit 3), so the row is kept for retry.
    assert reg.get(sid) is not None
    # ...but on the daemon side the teardown completed and the session is
    # terminal — every ordinary verb (ensureExecutor) is refused from now on.
    assert session_create._end_daemon_session(record) is False


async def _noop() -> None:
    return None
