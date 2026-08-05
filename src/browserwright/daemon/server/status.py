"""The daemon's pulse: one read-only snapshot of who is waiting on what.

Motivation (C1). A real hang looked like this from the outside: no error in the
daemon log, a client that gave up after 10.17s, and `/__status__` reporting
`extensions=1, tab_count=1` the whole way through. Every command an operator
could run said "healthy". The one thing that was actually true — a future left
in `_ExtensionConn.pending`, never resolved — was visible to nothing, and
`_request`'s `finally` popped it on the way out so even a late response left no
trace.

Meanwhile `ExecutorRegistry.all_handles()` had been returning session id, pid,
socket path and idle seconds since it was written, with two test files as its
only callers. The data was already there. This module is the wire out.

Design: **read-only and total.** It touches no locks, mutates nothing, and
answers with the same shape whether the daemon is fully wired or a bare `Router`
in a unit test. Every field is derived from an object that already existed —
this module invents no state of its own, which is what makes it safe to call
from an RPC handler on a daemon that may already be sick.

Clocks: `elapsed_s` / `age_s` fields come from `time.monotonic` deltas taken at
their source (they measure durations). `*_wall` fields are `time.time` (they
name moments a human reads). The two are never mixed in one number.
"""
from __future__ import annotations

import os
import time
from typing import Any

from .. import __version__

#: Bump when a consumer-visible key changes meaning. `browserwright-daemon ps`
#: reads this before trusting the rest.
SCHEMA_VERSION = 1


class _BareContext:
    """Stand-in for an `UpstreamContext` when only a `DaemonState` is reachable.

    A `Router` does not hold a back-pointer to the context that owns it, and in
    unit tests there is no `Daemon` at all. Rather than let `status` be the one
    verb that answers differently depending on how the daemon was assembled,
    every caller gets a context-shaped object.
    """

    __slots__ = ("state", "backend", "session_id", "holder")

    def __init__(self, state: object):
        self.state = state
        self.backend = getattr(state, "backend_name", None)
        self.session_id = None
        self.holder = None


def snapshot(daemon: object | None, *, state: object | None = None) -> dict:
    """Assemble the whole in-flight picture.

    ``daemon`` is the global :class:`~.daemon.Daemon` (or None). ``state`` is
    the calling router's own :class:`~.state.DaemonState`, used as the fallback
    when there is no daemon back-reference — a bare `Router` built by a unit
    test still gets an honest, same-shaped answer instead of an error.
    """
    contexts = _contexts_of(
        daemon, _BareContext(state) if state is not None else None)
    return {
        "schema_version": SCHEMA_VERSION,
        "daemon": _daemon_row(daemon),
        "contexts": [_context_row(c) for c in contexts],
        "executors": _executor_rows(daemon),
        "relay": _relay_row(contexts),
    }


# ---- pieces ----------------------------------------------------------------


def _contexts_of(daemon: object | None, context: object | None) -> list:
    all_contexts = getattr(daemon, "all_contexts", None)
    if callable(all_contexts):
        try:
            return list(all_contexts())
        except Exception:  # noqa: BLE001 - status must never be the thing that fails
            pass
    return [context] if context is not None else []


def _daemon_row(daemon: object | None) -> dict:
    return {
        "pid": os.getpid(),
        "version": __version__,
        "wired": daemon is not None,
        "now_wall": round(time.time(), 3),
    }


def _context_row(ctx: object) -> dict:
    state = getattr(ctx, "state", None)
    now_mono = time.monotonic()
    now_wall = time.time()
    row: dict[str, Any] = {
        "backend": getattr(ctx, "backend", None) or _attr(state, "backend_name"),
        # None on the shared context, the rdp session id otherwise.
        "session_id": getattr(ctx, "session_id", None),
        "upstream_phase": _enum_value(_attr(state, "upstream_phase")),
        "upstream_ws_url": _attr(state, "upstream_ws_url"),
        "last_close_reason": _attr(state, "last_close_reason"),
        "clients": [],
        "pending_requests": [],
        "attachers": 0,
        "targets": 0,
        "idle_s": None,
    }
    if state is None:
        return row
    last_activity = _attr(state, "last_activity_at")
    if isinstance(last_activity, (int, float)):
        row["idle_s"] = round(max(0.0, now_wall - last_activity), 3)
    row["attachers"] = len(getattr(state, "attachers", ()) or ())
    row["targets"] = len(getattr(state, "targets", ()) or ())
    row["clients"] = [
        _client_row(c, now_wall) for c in (getattr(state, "clients", {}) or {}).values()
    ]
    row["pending_requests"] = _pending_rows(state, now_mono)
    return row


def _client_row(client: object, now_wall: float) -> dict:
    return {
        "client_id": getattr(client, "client_id", None),
        "label": getattr(client, "label", ""),
        "session_id": getattr(client, "session_id", None),
        "session_name": getattr(client, "session_name", None),
        "sessions": len(getattr(client, "sessions", ()) or ()),
        # Frames the client sent while upstream was still opening. A non-zero
        # value on a client that looks stuck means it is waiting on the lazy
        # upstream open, not on the browser.
        "pre_open_buffered": len(getattr(client, "pre_open_buffer", ()) or ()),
        "connected_age_s": _age(getattr(client, "connected_at", None), now_wall),
        "last_command_age_s": _age(
            getattr(client, "last_command_at", None), now_wall),
    }


def _pending_rows(state: object, now_mono: float) -> list[dict]:
    """One row per client request still awaiting its upstream response.

    Sorted oldest-first: the row that matters in a hang is always the top one.
    """
    rows: list[dict] = []
    for upstream_id, pending in (getattr(state, "pending_requests", {}) or {}).items():
        elapsed = getattr(pending, "elapsed_s", None)
        rows.append({
            "hop": "router",
            "upstream_id": upstream_id,
            "client_id": getattr(pending, "client_id", None),
            "client_request_id": getattr(pending, "client_request_id", None),
            "method": getattr(pending, "method", ""),
            "attach_target_id": getattr(pending, "attach_target_id", None),
            "elapsed_s": (round(elapsed(now=now_mono), 3)
                          if callable(elapsed) else None),
        })
    rows.sort(key=lambda r: -(r["elapsed_s"] or 0.0))
    return rows


def _executor_rows(daemon: object | None) -> list[dict]:
    """Every resident executor, plus what its worker is running right now.

    `all_handles()` already knew the pid, the socket and the idle clock; the
    `inflight` key is the part the daemon genuinely cannot know on its own — the
    executor data plane bypasses the daemon entirely (Fork 2), so the worker
    publishes it to a sidecar file and we read that."""
    registry = getattr(daemon, "executors", None)
    all_handles = getattr(registry, "all_handles", None)
    if not callable(all_handles):
        return []
    try:
        handles = list(all_handles())
    except Exception:  # noqa: BLE001
        return []
    now_mono = time.monotonic()
    rows = []
    for h in handles:
        session_id = getattr(h, "session_id", None)
        proc = getattr(h, "proc", None)
        spawned_at = getattr(h, "spawned_at", None)
        rows.append({
            "session_id": session_id,
            "executor_id": getattr(h, "executor_id", None),
            "pid": getattr(proc, "pid", None),
            "alive": bool(h.is_alive()) if hasattr(h, "is_alive") else None,
            "sock": getattr(h, "sock_path", None),
            "age_s": (round(max(0.0, now_mono - spawned_at), 3)
                      if isinstance(spawned_at, (int, float)) else None),
            "idle_s": (round(h.idle_seconds(), 3)
                       if hasattr(h, "idle_seconds") else None),
            "inflight": _executor_inflight(session_id),
        })
    rows.sort(key=lambda r: str(r["session_id"]))
    return rows


def _executor_inflight(session_id: object) -> dict | None:
    if not isinstance(session_id, str):
        return None
    from .. import _ipc

    entry = _ipc.read_executor_inflight(session_id)
    if entry is None:
        return None
    started = entry.get("started_wall")
    if isinstance(started, (int, float)):
        entry["elapsed_s"] = round(max(0.0, time.time() - started), 3)
    return entry


def _relay_row(contexts: list) -> dict:
    """Extension-relay hop: per-connection pending counts + the awaited calls.

    Only the shared extension context owns a relay today; we iterate every
    context anyway so a future relay-bearing context can't hide."""
    extensions: list[dict] = []
    inflight: list[dict] = []
    running = False
    for ctx in contexts:
        relay = getattr(getattr(ctx, "holder", None), "relay", None)
        if relay is None:
            continue
        running = True
        try:
            payload = relay.status_payload()
            extensions.extend(payload.get("extension_details") or [])
        except Exception:  # noqa: BLE001
            pass
        try:
            inflight.extend(relay.inflight_snapshot())
        except Exception:  # noqa: BLE001
            pass
    return {"running": running, "extensions": extensions, "inflight": inflight}


# ---- tiny helpers ----------------------------------------------------------


def _attr(obj: object | None, name: str):
    return getattr(obj, name, None) if obj is not None else None


def _enum_value(v):
    return getattr(v, "value", v)


def _age(then: object, now: float) -> float | None:
    if not isinstance(then, (int, float)):
        return None
    return round(max(0.0, now - then), 3)
