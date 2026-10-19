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

A *replacement* daemon is not a failure, though: it adopts the session's
executor and its browser. So the two seams that run strictly before the
request can reach the executor — the ``ensureExecutor`` verb and the ``/exec``
connect — retry a daemon that went away, re-resolving the endpoint on every
attempt (`_call_until_daemon_returns`). When those retries run out, the text
names the daemon and what happened to it, because the alternative — blaming a
missing extension for a browser that is connected and healthy — is what sent
three agent calls to `chrome://extensions` on 2026-10-19 (ADR-0013 rule 3).
"""
from __future__ import annotations

import json
import signal
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import TypeVar

from websockets.exceptions import ConnectionClosed, WebSocketException
from websockets.sync.client import connect as ws_connect

from .. import session_registry as reg
from ..daemon.errors import DaemonShuttingDown
from ..daemon_url import daemon_endpoint
from ..errors import (
    BrowserwrightError,
    CDPError,
    CDPTransportClosed,
    DaemonUnavailable,
)
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

#: How long a *departed* daemon gets to come back before the client stops
#: retrying and says what happened. A replacement daemon needs the old one
#: stopped and its own endpoint bound before it can adopt the session's
#: executor, so this window is the whole difference between surviving a daemon
#: replacement and an `ExecutorUnavailable` that blames the wrong layer. It is
#: deliberately the same budget as the executor's cold-start connect
#: (13 x 0.75s ≈ 9s, `_executor/process.py`) so the two waits on one call cannot
#: stack into an apparent hang, and ADR-0014 caps it further by whatever the
#: caller's `--timeout` has left.
_DAEMON_RETURN_ATTEMPTS = 13
_DAEMON_RETURN_BACKOFF_S = 0.75

#: What `ws_connect` raises when the daemon's front door is not open (yet):
#: nothing listening, or nothing speaking the ws handshake.
_DAEMON_DOOR_ERRORS = (WebSocketException, ConnectionClosed, OSError)

_T = TypeVar("_T")


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


def _ensure_executor_lease(sess, *, deadline: float | None = None) -> ExecutorLease:
    """Ask the daemon to ensure the session's executor and return its identity.

    Uses the session's mode_b CDP client (``sess.cdp``) to send the
    control-plane verb. ``deadline`` is the caller's ``time.monotonic()``
    instant (ADR-0014), which caps the retries below.
    """
    sid = _session_id(sess)

    def _attempt() -> dict:
        # The browserwright session is already bound on the websocket query
        # (`?session=<id>`). Do not pass it as CDP's top-level `sessionId`;
        # that field means "attached target session" inside the proxy mux.
        return sess.cdp.send("BrowserwrightDaemon.ensureExecutor", bsSession=sid)

    def _give_up(exc: BaseException, attempts: int,
                 elapsed: float) -> BrowserwrightError:
        if isinstance(exc, DaemonUnavailable):
            # "nothing answered at <endpoint>" is already the better diagnosis
            # (probed, and it explains the auto-start rule). Wrapping it here
            # would bury that under a second, less specific story.
            return exc
        return ExecutorUnavailable(
            f"the daemon serving session {sid!r} went away mid-call (it was "
            f"replaced or is shutting down): {attempts} attempt(s) over "
            f"{elapsed:.1f}s could not reach it (last: {exc}). "
            f"{_endpoint_state_now()} The browser and the extension are not "
            f"involved; a replacement daemon adopts the session's executor.",
            fix="retry the command",
        )

    try:
        res = _call_until_daemon_returns(
            _attempt,
            deadline=deadline,
            departed=_daemon_departed,
            before_retry=lambda: _forget_daemon_endpoint(sess),
            give_up=_give_up,
        )
    except (DaemonUnavailable, ExecutorUnavailable):
        # "nothing answered at the endpoint" / "it left mid-call" already say
        # everything, with the daemon named; wrapping them would bury that
        # under a second, less specific fix about stale executors.
        raise
    except Exception as e:  # noqa: BLE001 - keep one agent-facing envelope
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


def _forget_daemon_endpoint(sess) -> None:
    """Make the next attempt dial the daemon that answers *now* (ADR-0013).

    ``ModeBClient.ws_url()`` caches its answer until ``invalidate()``, and
    `Session.cdp` re-creates its `CDPSession` only once the old one is closed —
    so both halves are needed: drop the cached url, and the dead socket is then
    replaced (and re-resolved) by the property itself rather than reused.
    Retrying over the socket that just died would retry the daemon that just
    left, which is the entire failure this absorbs.
    """
    invalidate = getattr(getattr(sess, "daemon", None), "invalidate", None)
    if callable(invalidate):
        invalidate()


def _daemon_departed(exc: BaseException, attempt: int) -> bool:
    """True only when ``exc`` says the *daemon* left — never on "any error".

    Three shapes qualify, and they are exactly the ways a client learns its
    daemon is gone: the request's socket died with no reply
    (`CDPTransportClosed`), the daemon answered in its own words that it is
    shutting down (`DaemonShuttingDown`, carried through the verb's JSON-RPC
    error envelope), or nothing answers the endpoint any more
    (`DaemonUnavailable`) *after* an earlier attempt already saw it leave.

    A refusal on the first attempt is deliberately NOT a departure: nothing was
    ever there, which `DaemonUnavailable` diagnoses on its own (probed endpoint,
    auto-start rule) and which `session new` / `recover` own. Retrying it would
    make every "no daemon is running" call 9s slower to say the same thing.

    Everything else — the no-extension diagnosis, a -32601 from a stale daemon,
    a rejected attach — would fail identically on the next attempt, so treating
    it as retryable would burn the caller's deadline and then report the wrong
    layer. That the daemon can tell these apart (`DaemonShuttingDown` vs the
    extension text) is what makes this list exist; do not flatten it here.
    """
    if isinstance(exc, CDPTransportClosed):
        return True
    if isinstance(exc, DaemonUnavailable):
        return attempt > 1
    if isinstance(exc, CDPError):
        return DaemonShuttingDown.MARKER in str(exc.cdp_message)
    return False


def _endpoint_is_the_daemons(exc: BaseException, _attempt: int) -> bool:
    """`_connect`'s classifier: the `/exec` surface lives only in the daemon.

    No request frame exists yet, so a refused/closed connect is always "the
    daemon is not answering yet" and never the browser or the extension — and a
    first-attempt refusal here still means the daemon left, because
    `ensureExecutor` reached that same daemon moments earlier. Anything that is
    not a transport failure (a broken URL resolution, a bad handshake argument)
    would fail identically on every attempt, so it is not retried.
    """
    return isinstance(exc, _DAEMON_DOOR_ERRORS)


def _endpoint_state_now() -> str:
    """ADR-0013 rule 3: probe the endpoint once and say what answered.

    "Nothing is listening" and "something that is not browserwright answered"
    are different diagnoses with different next steps, and neither may be
    guessed from a socket error — the case that rule was written for is a local
    proxy answering 503 on the endpoint while the daemon is fine. So the last
    words of a failed call carry the observation, not an inference.
    """
    from ..daemon import _ipc

    ep = daemon_endpoint()
    return f"Right now {_ipc.probe_endpoint_sync(ep.host, ep.port).describe()}."


def _daemon_return_budget(deadline: float | None) -> float:
    """~9s of retries, or less when the caller's deadline leaves less."""
    full = _DAEMON_RETURN_BACKOFF_S * _DAEMON_RETURN_ATTEMPTS
    if deadline is None:
        return full
    return max(0.0, min(full, deadline - time.monotonic()))


def _call_until_daemon_returns(
    attempt: Callable[[], _T],
    *,
    deadline: float | None,
    departed: Callable[[BaseException, int], bool],
    give_up: Callable[[BaseException, int, float], BrowserwrightError],
    before_retry: Callable[[], None] | None = None,
) -> _T:
    """Run ``attempt``, retrying only while the *daemon* is what went missing.

    **The retry scope is a correctness rule, not a convenience.** Both callers
    sit strictly before the session's request can have reached the executor:
    the `ensureExecutor` verb, and the `/exec` connect, which has no frame yet.
    Replaying either cannot execute the agent's code twice. The send/recv pair
    in `_run_request_on_executor` deliberately has no retry at all — once a
    request frame may be on the wire a replay would run the caller's code a
    second time, so that path reaps the exact executor instance instead.

    ``departed`` decides what counts as the daemon leaving, and sees the
    attempt number too (see `_daemon_departed`: a refused endpoint is the
    departure *continuing* only once an earlier attempt saw it leave).
    ``before_retry`` re-points the client at the endpoint that answers now.
    ``deadline`` (a ``time.monotonic()`` instant) caps the whole loop:
    ADR-0014 forbids an inner wait outliving the call deadline, and this wait
    is the innermost one on the call's path.
    """
    started = time.monotonic()
    budget_end = started + _daemon_return_budget(deadline)
    attempts = 0
    while True:
        attempts += 1
        try:
            return attempt()
        except Exception as e:  # noqa: BLE001 - classified on the next line
            if not departed(e, attempts):
                raise
            last = e
        remaining = budget_end - time.monotonic()
        if remaining <= 0 or attempts >= _DAEMON_RETURN_ATTEMPTS:
            final = give_up(last, attempts, time.monotonic() - started)
            if final is last:
                raise final
            raise final from last
        if before_retry is not None:
            before_retry()
        time.sleep(min(_DAEMON_RETURN_BACKOFF_S, remaining))


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
    # One absolute deadline for the pre-request work (ADR-0014): the retries in
    # `_ensure_executor_lease` / `_connect` are inner waits, so they are capped
    # by what the caller's `timeout_ms` leaves rather than by their own budget.
    deadline = time.monotonic() + max(request.timeout_ms, 1) / 1000.0
    lease = _ensure_executor_lease(sess, deadline=deadline)
    # The executor owns the exact outer deadline, including first-call
    # cold-start.  Give only a small framing/delivery margin so its terminal
    # response wins the race against the local socket timeout.
    recv_timeout = (
        max(request.timeout_ms, 1) / 1000.0 + _RESPONSE_DELIVERY_SLACK_S
    )
    conn = _connect(sid, timeout=recv_timeout, deadline=deadline)
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


def _connect(session_id: str, *, timeout: float = 30.0,
             deadline: float | None = None):
    """Open the data plane: a ws to the daemon's `/exec` relay.

    ``max_size`` must clear the executor's own frame cap — a screenshot-bearing
    response is legitimately large and the daemon relays it whole.

    A refusal here is the daemon's front door not being open, which is exactly
    what a *replacement* daemon looks like for a second or two while it binds
    and adopts the session's executor. Retrying is safe (no request frame
    exists yet, so nothing can run twice) and every attempt re-resolves the
    endpoint through `_exec_ws_url` — reading the state file the replacement
    publishes — so a daemon that came back on another port is dialed at its new
    address instead of the dead one. The executor-socket bind race the old
    client-side loop absorbed is still the *daemon's* problem
    (`exec_relay._dial_executor`); this loop is about the daemon itself, and it
    is capped by the caller's deadline (ADR-0014).
    """
    # The endpoint actually dialed last, for the failure message; the first
    # attempt fills it, so it is never empty when `_give_up` runs.
    attempted: list[str] = []

    def _open():
        attempted.append(_exec_ws_url(session_id))
        return ws_connect(
            attempted[-1],
            open_timeout=timeout,
            close_timeout=timeout,
            max_size=_MAX_FRAME,
            proxy=None,
            compression=None,
        )

    def _give_up(exc: BaseException, attempts: int,
                 elapsed: float) -> BrowserwrightError:
        return ExecutorUnavailable(
            f"could not open the executor data plane at {attempted[-1]} after "
            f"{attempts} attempt(s) over {elapsed:.1f}s: {exc}. The daemon "
            f"serving session {session_id!r} was replaced or is shutting "
            f"down; nothing reached the executor, so retrying is safe. "
            f"{_endpoint_state_now()}",
            fix="retry the command",
        )

    return _call_until_daemon_returns(
        _open,
        deadline=deadline,
        departed=_endpoint_is_the_daemons,
        give_up=_give_up,
    )


def _session_id(sess) -> str:
    rec = getattr(sess, "session_record", None)
    if isinstance(rec, dict) and rec.get("id"):
        return str(rec["id"])
    raise ExecutorUnavailable("no session id bound; cannot reach an executor")
