"""Thin-client side of the executor data plane.

Used by ``repl/inline.py`` when inline code touches ``page`` / ``context`` /
``snapshot`` / ``state`` / ``reset``: the whole code body is shipped to the
session's resident executor and the response is replayed locally.

Control plane (spawn + discover) goes through the daemon's
``BrowserwrightDaemon.ensureExecutor`` verb over the EXISTING mode_b socket
(tiny payload). The daemon spawns the executor if absent, waits for it to bind +
write its ``_ipc`` discovery file, and returns the socket path. The data plane
(this module) then connects DIRECTLY to that socket — keeping arbitrary code +
large output off the daemon's event loop (Fork 2).
"""
from __future__ import annotations

import socket
import time

from ..errors import BrowserwrightError
from .. import session_registry as reg
from .protocol import (
    DEFAULT_TIMEOUT_MS,
    ExecuteRequest,
    ExecuteResponse,
    recv_message,
    send_message,
)

# Generous slack added on TOP of the per-call timeout for the data-plane recv.
# The FIRST execute on a freshly-spawned executor triggers the lazy cold-start
# (connect_over_cdp + bind), which can take ~10-35s on a daemon-restart race
# (the executor's `_COLD_START_CONNECT_ATTEMPTS` backoff is ~10s; the registry's
# spawn-ready budget is 35s). The control-plane RPC no longer waits on that, so
# the wait moved HERE — the client's own blocking socket has no keepalive, so a
# long first call is fine as long as we don't time the recv out prematurely.
_COLD_START_RECV_SLACK_S = 45.0


class ExecutorUnavailable(BrowserwrightError):
    """The session's executor could not be ensured/reached.

    Surfaced when ``ensureExecutor`` fails or the executor socket can't be
    connected — actionable: the daemon must be running (it spawns the
    executor)."""

    default_fix = ("ensure the daemon is running (`browserwright-daemon status "
                   "--json` should show `alive`); if the executor is stale, "
                   "call `reset()` from inline code or run `browserwright "
                   "session reset <id>` before retrying.")


def ensure_executor(sess) -> str:
    """Ask the daemon to ensure the session's executor and return its socket
    path. Uses the session's mode_b CDP client (``sess.cdp``) to send the
    control-plane verb."""
    sid = _session_id(sess)
    try:
        # The browserwright session is already bound on the websocket query
        # (`?session=<id>`). Do not pass it as CDP's top-level `sessionId`;
        # that field means "attached target session" inside the proxy mux.
        res = sess.cdp.send(
            "BrowserwrightDaemon.ensureExecutor", bsSession=sid)
    except Exception as e:  # noqa: BLE001
        raise ExecutorUnavailable(
            f"ensureExecutor failed for session {sid!r}: {e}") from e
    sock_path = res.get("exec_sock") if isinstance(res, dict) else None
    if not isinstance(sock_path, str) or not sock_path:
        raise ExecutorUnavailable(
            f"ensureExecutor returned no socket for session {sid!r}: {res!r}")
    return sock_path


def run_on_executor(sess, code: str, *,
                    timeout_ms: int = DEFAULT_TIMEOUT_MS) -> ExecuteResponse:
    """Ship ``code`` to the session's executor and return its response.

    Ensures the executor (control plane), connects its socket (data plane),
    sends one :class:`ExecuteRequest`, reads one :class:`ExecuteResponse`.

    The recv socket timeout is the per-call ``timeout_ms`` PLUS a cold-start
    slack: the first execute on a fresh executor performs the lazy
    connect_over_cdp + bind (moved off the control plane), which can add up to
    ~35s. The executor itself bounds the worker per-call timeout; this slack
    only prevents the CLIENT recv from giving up before the executor replies."""
    sid = _session_id(sess)
    # Session idle is "time since the last user/agent instruction", not
    # executor process liveness. Touch before contacting the executor so a
    # wedged or long-running executor cannot prevent the durable idle clock
    # from reflecting that a new instruction arrived.
    reg.touch(sid)
    sock_path = ensure_executor(sess)
    recv_timeout = max(timeout_ms, 1) / 1000.0 + _COLD_START_RECV_SLACK_S
    conn = _connect(sock_path, timeout=recv_timeout)
    try:
        send_message(conn, ExecuteRequest(code=code, timeout_ms=timeout_ms).to_dict())
        msg = recv_message(conn)
    except (ConnectionError, OSError, ValueError) as e:
        raise ExecutorUnavailable(
            f"executor data-plane error on {sock_path!r}: {e}") from e
    finally:
        try:
            conn.close()
        except OSError:
            pass
    return ExecuteResponse.from_dict(msg)


def _connect(sock_path: str, *, timeout: float = 30.0,
             retry_until: float = 5.0) -> socket.socket:
    """Connect the executor's unix socket, briefly retrying a not-yet-bound
    socket (the daemon returns the path the moment it spawns; the bind may race
    by a few ms)."""
    deadline = time.monotonic() + retry_until
    last: OSError | None = None
    while True:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(timeout)
        try:
            s.connect(sock_path)
            return s
        except OSError as e:
            last = e
            s.close()
            if time.monotonic() >= deadline:
                raise ExecutorUnavailable(
                    f"could not connect executor socket {sock_path!r}: {e}"
                ) from last
            time.sleep(0.05)


def _session_id(sess) -> str:
    rec = getattr(sess, "session_record", None)
    if isinstance(rec, dict) and rec.get("id"):
        return str(rec["id"])
    raise ExecutorUnavailable("no session id bound; cannot reach an executor")
