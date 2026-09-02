"""Thin-client side of the executor data plane.

Used by ``repl/inline.py`` when inline code touches ``page`` / ``context`` /
``snapshot`` / ``state`` / ``reset``: the whole code body is shipped to the
session's resident executor and the response is replayed locally.

Both planes ride the daemon's one TCP endpoint (ADR-0011):

  - **control plane** — ``BrowserwrightDaemon.ensureExecutor`` over the existing
    mode_b control-surface ws (tiny payload). The daemon spawns the executor if
    absent, waits for it to bind + write its ``_ipc`` discovery file, and
    answers with a readiness confirmation plus the executor's instance identity.
    It no longer hands back a socket path.
  - **data plane** — a websocket to ``<endpoint>/exec?session=<id>``, which the
    daemon relays to the executor's own unix socket. That socket is now a
    daemon-internal detail, which is what makes a client on another machine
    possible at all.

The cost, accepted in ADR-0011: execute payloads and large outputs cross the
daemon's event loop, and a daemon restart severs a live data plane (it used to
survive one). The client turns that severing into ``ExecutorUnavailable`` with a
"daemon restarted or unreachable" message rather than a bare ws error.
"""
from __future__ import annotations

import json
import signal
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass

from websockets.exceptions import ConnectionClosed, WebSocketException
from websockets.sync.client import connect as ws_connect

from .. import session_registry as reg
from ..daemon_url import daemon_endpoint
from ..errors import BrowserwrightError, DaemonUnavailable
from .protocol import (
    DEFAULT_TIMEOUT_MS,
    _MAX_FRAME,
    ExecuteRequest,
    ExecuteResponse,
    TaskEnvelope,
)

# Allow the executor's deadline response a small framing/delivery margin.  The
# browser cold-start is part of the request deadline, not hidden extra time.
_RESPONSE_DELIVERY_SLACK_S = 2.0


class ExecutorUnavailable(BrowserwrightError):
    """The session's executor could not be ensured/reached.

    Surfaced when ``ensureExecutor`` fails or the executor socket can't be
    connected — actionable: the daemon must be running (it spawns the
    executor)."""

    default_fix = ("run `browserwright recover --session <id>`; it diagnoses "
                   "the daemon, tab, and executor in order and reports the "
                   "one layer that still needs human attention")


@dataclass(frozen=True)
class ExecutorLease:
    """One confirmed-ready executor.

    Identity only — no socket path. The client reaches the executor through the
    daemon's `/exec` relay keyed on the session id, and needs ``executor_id``
    solely to ask the daemon to reap *that exact process* (never a newer one
    that happens to have taken its place).
    """

    session_id: str
    executor_id: str


def _ensure_executor_lease(sess) -> ExecutorLease:
    """Ask the daemon to ensure the session's executor and return its socket
    path. Uses the session's mode_b CDP client (``sess.cdp``) to send the
    control-plane verb."""
    sid = _session_id(sess)
    try:
        # The browserwright session is already bound on the websocket query
        # (`?session=<id>`). Do not pass it as CDP's top-level `sessionId`;
        # that field means "attached target session" inside the proxy mux.
        res = sess.cdp.send("BrowserwrightDaemon.ensureExecutor", bsSession=sid)
    except DaemonUnavailable:
        # "the daemon is not there" already says everything, with the endpoint
        # named and the auto-start rule explained. Wrapping it would bury that
        # under a second, less specific fix about stale executors.
        raise
    except Exception as e:
        raise ExecutorUnavailable(
            f"ensureExecutor failed for session {sid!r}: {e}"
        ) from e
    ready = res.get("ready") if isinstance(res, dict) else None
    executor_id = res.get("executor_id") if isinstance(res, dict) else None
    if ready is not True:
        raise ExecutorUnavailable(
            f"ensureExecutor did not confirm readiness for session {sid!r}: "
            f"{res!r}"
        )
    if not isinstance(executor_id, str) or not executor_id:
        raise ExecutorUnavailable(
            "ensureExecutor returned no executor instance identity; run "
            "`browserwright recover --session <id>` before retrying"
        )
    return ExecutorLease(session_id=sid, executor_id=executor_id)


def ensure_executor(sess) -> str:
    """Ensure the session's executor and return its instance id."""
    return _ensure_executor_lease(sess).executor_id


def run_on_executor(
    sess,
    code: str,
    *,
    timeout_ms: int = DEFAULT_TIMEOUT_MS,
    env: dict[str, str] | None = None,
) -> ExecuteResponse:
    """Ship ``code`` to the session's executor and return its response.

    Ensures the executor (control plane), connects its socket (data plane),
    sends one :class:`ExecuteRequest`, reads one :class:`ExecuteResponse`.

    The executor enforces ``timeout_ms`` across cold-start and user code.  A
    terminal deadline/reset response is not returned to the caller until the
    daemon confirms that exact executor instance has exited."""
    return _run_request_on_executor(
        sess,
        ExecuteRequest(
            code=code,
            timeout_ms=timeout_ms,
            env=dict(env or {}),
        ),
    )


def run_task_on_executor(
    sess,
    site: str,
    name: str,
    *,
    args: dict | None = None,
    isolated: bool = False,
    timeout_ms: int = DEFAULT_TIMEOUT_MS,
    env: dict[str, str] | None = None,
) -> ExecuteResponse:
    """Run one validated site-skill task on the resident executor surface."""
    return _run_request_on_executor(
        sess,
        ExecuteRequest(
            code="",
            timeout_ms=timeout_ms,
            env=dict(env or {}),
            task=TaskEnvelope(
                site=site,
                name=name,
                args=dict(args or {}),
                isolated=isolated,
            ),
        ),
    )


def _run_request_on_executor(
    sess,
    request: ExecuteRequest,
) -> ExecuteResponse:
    """Send one request through the shared lease/reap data-plane lifecycle."""
    # Validate/copy before touching the daemon or opening a socket.  The
    # executor validates again at the trust boundary.
    request = ExecuteRequest.from_dict(request.to_dict())
    sid = _session_id(sess)
    # Session idle is "time since the last user/agent instruction", not
    # executor process liveness. Touch before contacting the executor so a
    # wedged or long-running executor cannot prevent the durable idle clock
    # from reflecting that a new instruction arrived.
    reg.touch(sid)
    lease = _ensure_executor_lease(sess)
    # The executor owns the exact outer deadline, including first-call
    # cold-start.  Give only a small framing/delivery margin so its terminal
    # response wins the race against the local socket timeout.
    recv_timeout = (
        max(request.timeout_ms, 1) / 1000.0 + _RESPONSE_DELIVERY_SLACK_S
    )
    conn = _connect(sid, timeout=recv_timeout)
    sent = False
    interrupted: BaseException | None = None
    transport_error: Exception | None = None
    msg: dict | None = None
    try:
        with _sigterm_as_system_exit():
            request.executor_id = lease.executor_id
            _send_frame(conn, request.to_dict())
            sent = True
            msg = _recv_frame(conn, timeout=recv_timeout)
    except (KeyboardInterrupt, SystemExit) as e:
        interrupted = e
    except (WebSocketException, ConnectionError, OSError, ValueError) as e:
        transport_error = e
    finally:
        try:
            conn.close()
        except Exception:  # noqa: BLE001 - close is best-effort
            pass

    # Close the data plane before asking the daemon to wait for process death.
    if interrupted is not None:
        if sent:
            _best_effort_recycle(sess, lease.executor_id)
        raise interrupted
    if transport_error is not None:
        recycle_error: Exception | None = None
        if sent:
            try:
                _confirm_recycled(sess, lease.executor_id)
            except Exception as cleanup_error:  # noqa: BLE001
                recycle_error = cleanup_error
        suffix = (
            f"; executor reap could not be confirmed: {recycle_error}"
            if recycle_error is not None
            else ""
        )
        raise ExecutorUnavailable(
            f"executor data-plane error for session {sid!r} over "
            f"{_exec_ws_url(sid)}: {transport_error}{suffix}. "
            f"{_SEVERED_HINT}"
        ) from transport_error
    if msg is None:
        raise ExecutorUnavailable(
            f"executor returned no response for session {sid!r}. "
            f"{_SEVERED_HINT}"
        )
    try:
        response = ExecuteResponse.from_dict(msg)
    except (TypeError, ValueError) as e:
        try:
            _confirm_recycled(sess, lease.executor_id)
        except Exception as cleanup_error:  # noqa: BLE001
            raise ExecutorUnavailable(
                "executor returned a malformed response and its reap could "
                f"not be confirmed: {cleanup_error}"
            ) from e
        raise ExecutorUnavailable(
            f"executor returned a malformed response: {e}"
        ) from e
    if response.terminal_reason is not None:
        _confirm_recycled(sess, lease.executor_id)
    return response


def _confirm_recycled(sess, executor_id: str) -> dict:
    """Ask the daemon to reap one exact executor and wait for process death."""
    sid = _session_id(sess)
    result = sess.cdp.send(
        "BrowserwrightDaemon.killExecutor",
        bsSession=sid,
        executorId=executor_id,
        wait=True,
    )
    if not isinstance(result, dict) or result.get("reaped") is not True:
        raise ExecutorUnavailable(
            f"daemon did not confirm executor {executor_id!r} was reaped: {result!r}"
        )
    return result


def _best_effort_recycle(sess, executor_id: str) -> None:
    try:
        _confirm_recycled(sess, executor_id)
    except BaseException:  # noqa: BLE001 - cannot mask the original interrupt
        return


@contextmanager
def _sigterm_as_system_exit() -> Iterator[None]:
    """Make SIGTERM unwind the blocking recv so exact-instance reap is tried."""
    if (
        not hasattr(signal, "SIGTERM")
        or threading.current_thread() is not threading.main_thread()
    ):
        yield
        return
    previous = signal.getsignal(signal.SIGTERM)

    def _handle(signum, _frame):
        raise SystemExit(128 + int(signum))

    try:
        signal.signal(signal.SIGTERM, _handle)
        yield
    finally:
        signal.signal(signal.SIGTERM, previous)


#: What a severed `/exec` relay almost always means. The daemon owns both ends
#: of that relay, so when it goes the plane goes with it — a failure mode that
#: did not exist while the client dialed the executor socket directly.
_SEVERED_HINT = (
    "the daemon was restarted or became unreachable mid-call, which severs the "
    "executor data plane; retry the command"
)


def _exec_ws_url(session_id: str) -> str:
    """The endpoint's exec-relay URL for one session."""
    return daemon_endpoint().ws("/exec", session=session_id)


def _send_frame(ws, payload: dict) -> None:
    """One request object = one ws text message (the ws frame IS the framing)."""
    data = json.dumps(payload)
    if len(data.encode("utf-8")) > _MAX_FRAME:
        raise ValueError(f"executor request exceeds {_MAX_FRAME} bytes")
    ws.send(data)


def _recv_frame(ws, timeout: float | None = None) -> dict:
    """Block for the executor's single response frame.

    ``timeout`` is only a delivery backstop: the executor owns the authoritative
    deadline and answers terminally when it expires, so this is set slightly
    wider so that its own answer wins the race."""
    raw = ws.recv(timeout=timeout)
    text = raw.decode("utf-8") if isinstance(raw, (bytes, bytearray)) else raw
    msg = json.loads(text)
    if not isinstance(msg, dict):
        raise ValueError(f"executor returned a non-object frame: {type(msg)}")
    return msg


def _connect(session_id: str, *, timeout: float = 30.0):
    """Open the data plane: a ws to the daemon's `/exec` relay.

    ``max_size`` must clear the executor's own frame cap — a screenshot-bearing
    response is legitimately large and the daemon relays it whole.

    No connect retry loop here any more: the daemon accepted our
    `ensureExecutor` a moment ago, so it is listening, and the executor-socket
    bind race the old retry absorbed is now the *daemon's* problem, handled
    inside `exec_relay`. A refusal here means the daemon itself is gone.
    """
    url = _exec_ws_url(session_id)
    try:
        return ws_connect(
            url,
            open_timeout=timeout,
            close_timeout=timeout,
            max_size=_MAX_FRAME,
            proxy=None,
            compression=None,
        )
    except (WebSocketException, ConnectionClosed, OSError) as e:
        raise ExecutorUnavailable(
            f"could not open the executor data plane at {url}: {e}"
        ) from e


def _session_id(sess) -> str:
    rec = getattr(sess, "session_record", None)
    if isinstance(rec, dict) and rec.get("id"):
        return str(rec["id"])
    raise ExecutorUnavailable("no session id bound; cannot reach an executor")
