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

import signal
import socket
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass

from .. import session_registry as reg
from ..errors import BrowserwrightError
from .protocol import (
    DEFAULT_TIMEOUT_MS,
    ExecuteRequest,
    ExecuteResponse,
    TaskEnvelope,
    recv_message,
    send_message,
)

# Allow the executor's deadline response a small framing/delivery margin.  The
# browser cold-start is part of the request deadline, not hidden extra time.
_RESPONSE_DELIVERY_SLACK_S = 2.0


class ExecutorUnavailable(BrowserwrightError):
    """The session's executor could not be ensured/reached.

    Surfaced when ``ensureExecutor`` fails or the executor socket can't be
    connected — actionable: the daemon must be running (it spawns the
    executor)."""

    default_fix = ("ensure the daemon is running (`browserwright-daemon status "
                   "--json` should show `alive`); if the executor is stale, "
                   "call `reset()` as a standalone/final inline statement, "
                   "then retry in a new command; or run `browserwright "
                   "session reset <id>` before retrying.")


@dataclass(frozen=True)
class ExecutorLease:
    sock_path: str
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
    except Exception as e:
        raise ExecutorUnavailable(
            f"ensureExecutor failed for session {sid!r}: {e}"
        ) from e
    sock_path = res.get("exec_sock") if isinstance(res, dict) else None
    executor_id = res.get("executor_id") if isinstance(res, dict) else None
    if not isinstance(sock_path, str) or not sock_path:
        raise ExecutorUnavailable(
            f"ensureExecutor returned no socket for session {sid!r}: {res!r}"
        )
    if not isinstance(executor_id, str) or not executor_id:
        raise ExecutorUnavailable(
            "ensureExecutor returned no executor instance identity; restart "
            "the daemon so timeout cleanup cannot target a newer process"
        )
    return ExecutorLease(sock_path=sock_path, executor_id=executor_id)


def ensure_executor(sess) -> str:
    """Compatibility wrapper returning only the executor socket path."""
    return _ensure_executor_lease(sess).sock_path


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
    conn = _connect(lease.sock_path, timeout=recv_timeout)
    sent = False
    interrupted: BaseException | None = None
    transport_error: ConnectionError | OSError | ValueError | None = None
    msg: dict | None = None
    try:
        with _sigterm_as_system_exit():
            request.executor_id = lease.executor_id
            send_message(conn, request.to_dict())
            sent = True
            msg = recv_message(conn)
    except (KeyboardInterrupt, SystemExit) as e:
        interrupted = e
    except (ConnectionError, OSError, ValueError) as e:
        transport_error = e
    finally:
        try:
            conn.close()
        except OSError:
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
            f"executor data-plane error on {lease.sock_path!r}: "
            f"{transport_error}{suffix}"
        ) from transport_error
    if msg is None:
        raise ExecutorUnavailable(
            f"executor returned no response on {lease.sock_path!r}"
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
