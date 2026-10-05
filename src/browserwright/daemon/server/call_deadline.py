"""The **call deadline** as the daemon sees it (ADR-0014).

An agent's call reaches the daemon as one `ExecuteRequest` on the `/exec`
relay, and that request carries `timeout_ms`. The executor enforces it as a
fail-stop deadline. Everything the executor's Playwright then sends to the
browser crosses the daemon again — as CDP commands on the session's facade
connection — and those commands must not wait on a fixed budget of their own
that can expire first (#116). So the relay binds the call's absolute deadline
to the session for the lifetime of the call, and the facade asks how much of
it is left whenever it forwards a command for that session.

Daemon-internal work with no caller (attach on extension connect, teardown,
heartbeats — ADR-0009, ADR-0013) never asks, and keeps its own budgets.

The executor runs requests FIFO, so when several calls for one session are in
flight the one actually running is the OLDEST still bound; that is the
deadline `remaining_s` reports.
"""
from __future__ import annotations

import itertools
import json
import time

from ..._executor.protocol import ExecuteRequest

#: session id -> [(token, absolute monotonic deadline)], oldest first.
_bound: dict[str, list[tuple[int, float]]] = {}
_tokens = itertools.count(1)


def bind(session_id: str, timeout_ms: int) -> int:
    """Bind one call's absolute deadline to ``session_id``; returns a token
    for `release`."""
    token = next(_tokens)
    deadline = time.monotonic() + max(0, timeout_ms) / 1000.0
    _bound.setdefault(session_id, []).append((token, deadline))
    return token


def release(session_id: str, token: int) -> None:
    """Unbind one call. Releasing an unknown or already-released token is a
    no-op, so every exit path may release unconditionally."""
    calls = _bound.get(session_id)
    if not calls:
        return
    calls[:] = [c for c in calls if c[0] != token]
    if not calls:
        _bound.pop(session_id, None)


def remaining_s(session_id: str | None) -> float | None:
    """Seconds left on the call currently running for ``session_id``, or
    ``None`` when no agent call is bound (the caller then keeps its own
    budget). Never negative: an expired deadline reports 0.0 — the executor
    is about to be fail-stopped, and nothing should start a fresh wait."""
    if session_id is None:
        return None
    calls = _bound.get(session_id)
    if not calls:
        return None
    return max(0.0, calls[0][1] - time.monotonic())


#: How far past the call deadline an agent command's inner waits reach.
#:
#: The executor owns the call deadline: when it runs out the executor is
#: fail-stopped and the caller gets `DeadlineExceeded` (exit 7). An inner wait
#: that ended *at* the deadline would race that — and the extension, which by
#: construction settles before the relay, would win it, surfacing the expiry
#: as an operation timeout inside the agent's code (`OperationTimeout`, exit 8)
#: instead. So the inner waits are derived from the deadline but end this much
#: after it: the executor always fires first, and the inner bounds stay what
#: they are for — a net for a wedged extension or Chrome. The relay's margin
#: (at most 1s) is taken out of this, so the extension still outlasts the
#: deadline by at least 1s.
INNER_GRACE_S = 2.0


def command_wait_s(session_id: str | None) -> float | None:
    """How long an agent command for ``session_id`` may wait in the relay:
    the remaining call deadline plus `INNER_GRACE_S`, or ``None`` when no
    call is bound (the relay keeps its own default)."""
    remaining = remaining_s(session_id)
    return None if remaining is None else remaining + INNER_GRACE_S


def timeout_ms_of(payload: bytes) -> int | None:
    """The `timeout_ms` the executor will enforce for one request frame, or
    ``None`` when the frame is not a request it will run.

    Parsed with the executor's own `ExecuteRequest.from_dict`, so a missing or
    invalid value resolves to the same default the executor applies."""
    try:
        frame = json.loads(payload)
        return ExecuteRequest.from_dict(frame).timeout_ms
    except (ValueError, TypeError, AttributeError):
        return None


class RelayedCalls:
    """The calls one `/exec` connection has in flight for its session.

    The relay feeds it every request frame it forwards to the executor and
    every response frame it forwards back: a request binds its deadline before
    the executor can act on it, its response releases it, and closing the
    connection releases whatever is left (a fail-stopped executor answers
    nothing). One response answers the oldest request, as the executor is
    FIFO.
    """

    def __init__(self, session_id: str) -> None:
        self._session_id = session_id
        self._tokens: list[int] = []

    def request(self, payload: bytes) -> None:
        timeout_ms = timeout_ms_of(payload)
        if timeout_ms is not None:
            self._tokens.append(bind(self._session_id, timeout_ms))

    def response(self) -> None:
        if self._tokens:
            release(self._session_id, self._tokens.pop(0))

    def close(self) -> None:
        while self._tokens:
            release(self._session_id, self._tokens.pop())
