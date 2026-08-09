"""Issue #57 — what counts as "someone is using the daemon".

A restart kills every session's live executor state at once. The agent that
loses it sees only "my page is gone" and cannot tell that a *different* agent
restarted the daemon underneath it — so its natural next move is to restart too.
The gate exists to break that loop.

The gate's whole value depends on it being *closed rarely*. `idle_close_after`
defaults to None, so executors are never idle-reaped and "has a live executor"
stays true forever after a single command; a gate that is always closed teaches
every agent to pass `--force` unconditionally, which is the same as no gate.
That is why the predicate reuses the ledger's own idle clock rather than
executor liveness — see `restart_guard`'s docstring and CONTEXT.md `idle clock`.
"""
from __future__ import annotations

import time

from browserwright.daemon import restart_guard


NOW = 1_000_000.0


def _snapshot(*, executors=None, relay_inflight=None, clients=None,
              pending=None) -> dict:
    return {
        "executors": executors or [],
        "relay": {"running": True, "extensions": [],
                  "inflight": relay_inflight or []},
        "contexts": [{
            "clients": clients or [],
            "pending_requests": pending or [],
        }],
    }


def _probe(**kw):
    kw.setdefault("snapshot", _snapshot())
    kw.setdefault("sessions", [])
    kw.setdefault("now", NOW)
    return restart_guard.probe(None, **kw)


# ---- not activity ----------------------------------------------------------


def test_idle_daemon_is_not_blocked():
    assert _probe().blocked is False


def test_connected_extension_alone_is_not_activity():
    """The user's Chrome is always connected. Counting it closes the gate 100%
    of the time, which is the failure mode that makes the whole flag useless."""
    snap = _snapshot()
    snap["relay"]["extensions"] = [{"install_id": "bd-1", "pending": 0}]
    assert _probe(snapshot=snap).blocked is False


def test_sessions_without_live_executors_are_not_activity():
    """Session rows survive a restart by design — the ledger outlives every
    process. Losing nothing is not a reason to refuse."""
    act = _probe(
        snapshot=_snapshot(executors=[]),
        sessions=[{"id": "7", "last_seen": NOW - 1.0}])
    assert act.blocked is False


def test_stale_executor_does_not_block_forever():
    """The reason this is not "any live executor": with idle-reap off by
    default, that predicate never becomes false again."""
    act = _probe(
        snapshot=_snapshot(executors=[{"session_id": "7", "alive": True}]),
        sessions=[{"id": "7", "last_seen": NOW - 3600.0}])
    assert act.blocked is False
    assert act.determinate is True


def test_dead_executor_row_is_ignored():
    act = _probe(
        snapshot=_snapshot(executors=[{"session_id": "7", "alive": False}]),
        sessions=[{"id": "7", "last_seen": NOW - 1.0}])
    assert act.blocked is False


# ---- activity --------------------------------------------------------------


def test_recently_instructed_session_with_live_executor_blocks():
    act = _probe(
        snapshot=_snapshot(executors=[{"session_id": "7", "alive": True}]),
        sessions=[{"id": "7", "last_seen": NOW - 4.0}])
    assert act.blocked is True
    assert "7 (idle 4s)" in act.reasons[0]


def test_executor_running_code_right_now_blocks():
    """In-flight beats every clock: someone is waiting on an answer."""
    act = _probe(snapshot=_snapshot(
        executors=[{"session_id": "7", "alive": True,
                    "inflight": {"code": "page.click(...)"}}]))
    assert act.blocked is True
    assert any("running code right now" in r for r in act.reasons)


def test_relay_inflight_blocks():
    act = _probe(snapshot=_snapshot(relay_inflight=[{"method": "Page.navigate"}]))
    assert act.blocked is True
    assert any("extension-relay" in r for r in act.reasons)


def test_router_pending_requests_block():
    act = _probe(snapshot=_snapshot(pending=[{"method": "Target.attachToTarget"}]))
    assert act.blocked is True
    assert any("router request" in r for r in act.reasons)


def test_another_connected_client_blocks():
    act = _probe(snapshot=_snapshot(
        clients=[{"client_id": 4, "label": "skill-client"}]))
    assert act.blocked is True
    assert any("skill-client" in r for r in act.reasons)


def test_our_own_probe_connection_is_not_counted():
    """`_fetch_snapshot` pins `client_label="cli-restart"` precisely so the
    connection we opened to ask the question cannot answer it for us."""
    act = _probe(snapshot=_snapshot(
        clients=[{"client_id": 9, "label": "cli-restart"}]))
    assert act.blocked is False


# ---- boundaries ------------------------------------------------------------


def test_threshold_is_configurable():
    snap = _snapshot(executors=[{"session_id": "7", "alive": True}])
    sessions = [{"id": "7", "last_seen": NOW - 30.0}]
    assert _probe(snapshot=snap, sessions=sessions, active_within=10.0).blocked is False
    assert _probe(snapshot=snap, sessions=sessions, active_within=60.0).blocked is True


def test_zero_threshold_disables_the_session_limb_only():
    """`BD_RESTART_ACTIVE_WITHIN=0` opts out of the idle-clock heuristic but
    must not switch off the in-flight signals, which are never heuristic."""
    snap = _snapshot(executors=[{"session_id": "7", "alive": True,
                                 "inflight": {"code": "x"}}])
    act = _probe(snapshot=snap,
                 sessions=[{"id": "7", "last_seen": NOW}],
                 active_within=0.0)
    assert act.blocked is True
    assert all("live executor active" not in r for r in act.reasons)


def test_env_override_is_read(monkeypatch):
    monkeypatch.setenv("BD_RESTART_ACTIVE_WITHIN", "42")
    assert restart_guard.active_within_default() == 42.0


def test_bad_env_override_is_loud(monkeypatch):
    """A typo must not silently widen or disable the gate."""
    from browserwright.daemon.errors import UserError
    import pytest

    monkeypatch.setenv("BD_RESTART_ACTIVE_WITHIN", "5 minutes")
    with pytest.raises(UserError):
        restart_guard.active_within_default()


def test_unreachable_daemon_is_indeterminate_not_blocked(monkeypatch):
    """A daemon that cannot answer its own status RPC is serving nobody — and
    that is exactly when an agent legitimately needs to restart it."""
    monkeypatch.setattr(restart_guard, "_fetch_snapshot",
                        lambda cfg, *, timeout: None)
    act = restart_guard.probe(None, sessions=[], now=NOW)
    assert act.blocked is False
    assert act.determinate is False
    assert "did not answer" in act.reasons[0]


def test_probe_uses_wall_clock_by_default():
    """`last_seen` is `time.time()`, so the comparison must be too — mixing in
    a monotonic reading would make every session look ancient."""
    now = time.time()
    act = restart_guard.probe(
        None,
        snapshot=_snapshot(executors=[{"session_id": "7", "alive": True}]),
        sessions=[{"id": "7", "last_seen": now - 1.0}])
    assert act.blocked is True
