"""Focused contract tests for ADR-0013's per-session recovery machine."""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from browserwright.daemon.server.daemon import Daemon
from browserwright.daemon.server.exec_relay import _report_executor_result
from browserwright.daemon.server.extension_upstream import ExtensionUpstream
from browserwright.daemon.server.relay import RelayServer
from browserwright.daemon.server.session_state import (
    EXECUTOR_DEAD,
    EXECUTOR_EXITED,
    EXECUTOR_READY,
    EXECUTOR_REAPED,
    EXECUTOR_UNBOUND,
    EXTENSION_DISCONNECTED,
    EXTENSION_HELLO,
    EXTENSION_LOST,
    HEALTHY,
    NEEDS_HUMAN,
    RECOVERY_FAILED,
    SESSION_ENDED,
    TAB_GONE,
    TAB_RECOVERED,
    TAB_RECOVER_FAILED,
    RecoveryStateMachine,
)


def _machine(initial: str = EXECUTOR_UNBOUND):
    writes = []
    clock = iter(range(100, 200))
    machine = RecoveryStateMachine(
        persist=lambda sid, rec: writes.append((sid, rec)),
        now=lambda: next(clock),
    )
    machine.load(
        [{"id": "7", "backend": "cdp", "recovery": {"state": initial}}],
        extension_connected=True,
        executor_alive=lambda _sid: False,
    )
    return machine, writes


async def _noop(_value: str) -> None:
    return None


class _Relay:
    """Just enough relay for an adapter to open and read its generation."""

    def __init__(self, generation: int):
        self.connection_generation = generation

    async def wait_ready(self, timeout: float) -> None:
        return None

    def set_event_handler(self, handler) -> None:
        return None


async def _open_adapter(generation: int, machine) -> ExtensionUpstream:
    """An opened extension adapter wired to ``machine`` — target events are
    only acted on once a client has opened the browser."""
    adapter = ExtensionUpstream(_Relay(generation), _noop, _noop)
    adapter.bind_recovery(machine, lambda _sid: True)
    await adapter.open()
    return adapter

def test_transition_table_names_the_broken_layer_and_recovers():
    machine, _ = _machine()

    assert machine.note("7", EXECUTOR_READY) == HEALTHY
    assert machine.note("7", EXECUTOR_EXITED) == EXECUTOR_DEAD
    assert machine.note("7", EXECUTOR_READY) == HEALTHY
    assert machine.note("7", EXECUTOR_REAPED) == EXECUTOR_UNBOUND
    assert machine.note("7", TAB_RECOVER_FAILED) == TAB_GONE
    assert machine.note("7", TAB_RECOVERED, executor_alive=False) == EXECUTOR_UNBOUND
    assert machine.note("7", TAB_RECOVERED, executor_alive=True) == HEALTHY
    assert machine.note("7", RECOVERY_FAILED, reason="browser cannot start") == NEEDS_HUMAN


def test_extension_loss_dominates_late_tab_and_executor_events():
    machine, _ = _machine(HEALTHY)

    assert machine.note("7", EXTENSION_LOST, generation=4) == EXTENSION_DISCONNECTED
    assert machine.note("7", TAB_RECOVERED, generation=4,
                        executor_alive=True) == EXTENSION_DISCONNECTED
    assert machine.note("7", EXECUTOR_EXITED, generation=4) == EXTENSION_DISCONNECTED
    assert machine.state_of("7") == EXTENSION_DISCONNECTED

    assert machine.note("7", EXTENSION_HELLO, generation=5) == TAB_GONE
    assert machine.note("7", TAB_RECOVERED, generation=5,
                        executor_alive=True) == HEALTHY


def test_tab_failure_dominates_executor_reap_and_respawn():
    machine, _ = _machine(HEALTHY)

    assert machine.note("7", TAB_RECOVER_FAILED) == TAB_GONE
    assert machine.note("7", EXECUTOR_EXITED) == TAB_GONE
    assert machine.note("7", EXECUTOR_READY) == TAB_GONE


def test_exec_relay_reports_live_and_lost_tab_outcomes():
    machine, _ = _machine(HEALTHY)
    daemon = SimpleNamespace(recovery=machine)

    _report_executor_result(daemon, "7", json.dumps({
        "error": {"msg": "page disappeared"},
        "terminal_reason": "target_closed",
    }).encode())
    assert machine.state_of("7") == TAB_GONE

    _report_executor_result(daemon, "7", json.dumps({
        "error": None,
        "terminal_reason": None,
    }).encode())
    assert machine.state_of("7") == HEALTHY


def test_terminal_session_removes_recovery_state():
    machine, _ = _machine(HEALTHY)
    Daemon._mark_session_ended(SimpleNamespace(recovery=machine), "7")
    assert machine.get("7") is None


@pytest.mark.asyncio
async def test_extension_target_detach_reports_current_tab_gone(monkeypatch):
    machine = RecoveryStateMachine()
    machine.load(
        [{"id": "ext", "backend": "extension",
          "runtime": {"current_target_id": "ext-tab-42"},
          "recovery": {"state": HEALTHY}}],
        extension_connected=True, executor_alive=lambda _sid: True)
    adapter = await _open_adapter(3, machine)

    async def missing_tab(_sid):
        raise RuntimeError("group has no tab")

    adapter.recover_session = missing_tab
    monkeypatch.setattr(
        "browserwright.session_registry.list_all",
        lambda: [{"id": "ext", "backend": "extension",
                  "runtime": {"current_target_id": "ext-tab-42"}}])

    await adapter._on_target_event({
        "type": "detached", "tabId": 42, "_relay_generation": 3})

    assert machine.state_of("ext") == TAB_GONE


@pytest.mark.asyncio
async def test_extension_target_event_requires_live_group_and_fresh_generation(
    monkeypatch,
):
    machine = RecoveryStateMachine()
    machine.load(
        [{"id": "ext", "backend": "extension",
          "runtime": {"current_target_id": "ext-tab-42"},
          "recovery": {"state": HEALTHY}}],
        extension_connected=True, executor_alive=lambda _sid: True)
    machine.note("ext", EXTENSION_LOST, generation=5)

    class GroupAware:
        calls = 0

        async def recover_session(self, _sid):
            self.calls += 1
            return {"targetId": "ext-tab-42"}

        async def target_belongs_to_session(self, _sid, _target):
            self.calls += 1
            return False

    group = GroupAware()
    adapter = await _open_adapter(6, machine)
    adapter.recover_session = group.recover_session
    adapter.target_belongs_to_session = group.target_belongs_to_session
    monkeypatch.setattr(
        "browserwright.session_registry.list_all",
        lambda: [{"id": "ext", "backend": "extension",
                  "runtime": {"current_target_id": "ext-tab-42"}}])

    # Old connection event is discarded before it can perform recovery.
    await adapter._on_target_event({
        "type": "detached", "tabId": 42, "_relay_generation": 4})
    assert group.calls == 0
    assert machine.state_of("ext") == EXTENSION_DISCONNECTED

    # A current `attached` event without canonical group membership cannot
    # promote the session either.
    await adapter._on_target_event({
        "type": "attached", "tabId": 42, "_relay_generation": 6})
    assert group.calls == 1
    assert machine.state_of("ext") == EXTENSION_DISCONNECTED


@pytest.mark.asyncio
async def test_relay_fanout_captures_generation_before_task_runs():
    relay = RelayServer(port=0)
    seen = []

    async def observe(msg):
        seen.append(msg)

    relay.add_event_listener(observe)
    relay._connection_generation = 3
    relay._schedule_fanout_listeners(
        {"type": "detached", "tabId": 42}, generation=3)
    relay._connection_generation = 4
    await asyncio.sleep(0)

    assert seen[0]["_relay_generation"] == 3


@pytest.mark.asyncio
async def test_superseded_connection_event_keeps_its_own_generation():
    relay = RelayServer(port=0)
    seen = []

    async def observe(msg):
        seen.append(msg)

    relay.add_event_listener(observe)
    old = SimpleNamespace(connection_generation=3, tabs={})
    relay._connection_generation = 4

    await relay._dispatch_from_extension(
        old, "old", {"type": "detached", "tabId": 42})
    await asyncio.sleep(0)

    assert seen[0]["_relay_generation"] == 3


def test_older_connection_generation_cannot_overwrite_newer_loss():
    machine, writes = _machine(HEALTHY)
    machine.note("7", EXTENSION_LOST, generation=9, reason="new socket lost")
    before = len(writes)

    assert machine.note("7", TAB_RECOVERED, generation=8,
                        executor_alive=True) is None
    assert machine.get("7")["generation"] == 9
    assert machine.state_of("7") == EXTENSION_DISCONNECTED
    assert len(writes) == before


def test_load_reconciles_persisted_state_with_live_observations():
    rows = [
        {"id": "ext", "backend": "extension",
         "recovery": {"state": HEALTHY, "since": 10, "reason": "old"}},
        {"id": "adopted", "backend": "cdp",
         "recovery": {"state": EXECUTOR_DEAD, "since": 11}},
        {"id": "missing", "backend": "cdp",
         "recovery": {"state": HEALTHY, "since": 12}},
        {"id": "invalid", "backend": "cdp",
         "recovery": {"state": "invented"}},
    ]
    machine = RecoveryStateMachine(now=lambda: 99)
    machine.load(rows, extension_connected=False,
                 executor_alive=lambda sid: sid == "adopted")

    assert machine.state_of("ext") == EXTENSION_DISCONNECTED
    assert machine.state_of("adopted") == TAB_GONE
    assert machine.state_of("missing") == EXECUTOR_UNBOUND
    assert machine.state_of("invalid") == EXECUTOR_UNBOUND
    assert machine.get("ext")["since"] == 99


def test_load_keeps_a_connected_extension_session_recoverable():
    machine = RecoveryStateMachine(now=lambda: 99)
    machine.load(
        [{"id": "ext", "backend": "extension",
          "recovery": {"state": TAB_GONE, "since": 12}}],
        extension_connected=True,
        executor_alive=lambda _sid: False,
    )
    assert machine.state_of("ext") == TAB_GONE


def test_persistence_failure_never_breaks_a_transition(caplog):
    def broken_persist(_sid, _rec):
        raise OSError("disk full")

    machine = RecoveryStateMachine(persist=broken_persist, now=lambda: 10)
    assert machine.note("7", EXECUTOR_READY) == HEALTHY
    assert machine.state_of("7") == HEALTHY
    assert "could not persist state" in caplog.text


def test_session_end_removes_memory_and_persists_removal():
    machine, writes = _machine(HEALTHY)
    machine.note("7", SESSION_ENDED)

    assert machine.get("7") is None
    assert writes[-1] == ("7", None)
