"""Focused unit contracts for ADR-0013's explicit recovery path."""
from __future__ import annotations

import asyncio
import json
from subprocess import CompletedProcess
from types import SimpleNamespace

import pytest

from browserwright import cli as user_cli
from browserwright.daemon import launchagent
from browserwright.daemon._ipc import EndpointProbe
from browserwright.daemon.server.proxy import Router
from browserwright.daemon.server.session_state import (
    HEALTHY,
    NEEDS_HUMAN,
    TAB_RECOVERED,
    RecoveryStateMachine,
)
from browserwright.daemon.server.state import DaemonState, UpstreamPhase
from browserwright.daemon_url import DaemonEndpoint


class _Relay:
    def __init__(self, ready: bool):
        self.ready = ready
        self.wait_calls = 0

    @property
    def is_ready(self):
        return self.ready

    async def wait_ready(self, timeout):
        self.wait_calls += 1
        if not self.ready:
            raise asyncio.TimeoutError


class _Extension:
    def __init__(self, error: Exception | None = None):
        self.error = error
        self.calls = []

    async def recover_session(self, sid):
        self.calls.append(sid)
        if self.error:
            raise self.error
        return {"recovered": [1]}

    async def open_background_tab(self, url, *, session_id, background):
        self.calls.append(("open", url, session_id, background))
        return {"tabId": 2}


class _Handle:
    def __init__(self, alive=True):
        self.alive = alive

    def is_alive(self):
        return self.alive


class _Registry:
    def __init__(self, machine, *, alive=True, spawn_error=None):
        self.machine = machine
        self.handle = _Handle(alive) if alive else None
        self.spawn_error = spawn_error
        self.ensure_calls = []

    def get(self, _sid):
        return self.handle

    async def ensure_with_preflight(self, sid, preflight):
        self.ensure_calls.append(sid)
        await preflight()
        if self.spawn_error:
            raise self.spawn_error
        self.handle = _Handle(True)
        self.machine.note(sid, "executor_ready", executor_alive=True)
        return f"/tmp/{sid}.sock"


async def _invoke_recover(monkeypatch, *, backend="extension", ready=True,
                          tab_error=None, executor_alive=True, spawn_error=None,
                          probe_error=None):
    machine = RecoveryStateMachine()
    machine.load([{"id": "7", "backend": backend}],
                 extension_connected=ready,
                 executor_alive=lambda _sid: executor_alive)
    relay = _Relay(ready)
    extension = _Extension(tab_error)
    holder = SimpleNamespace(relay=relay, _extension_adapter=extension)
    registry = _Registry(machine, alive=executor_alive, spawn_error=spawn_error)
    daemon = SimpleNamespace(
        recovery=machine,
        executors=registry,
        shared_context=SimpleNamespace(holder=holder),
    )
    state = DaemonState(backend_name=backend)
    state.upstream_phase = UpstreamPhase.CONNECTED
    router = Router(state)
    router.daemon = daemon
    client = state.allocate_client("test")
    client.session_id = "7"
    replies = []

    async def send(text):
        replies.append(json.loads(text))

    router.register_client(client.client_id, send)
    monkeypatch.setattr(
        "browserwright.daemon.server.verbs.session_registry.get",
        lambda sid: {"id": sid, "backend": backend},
    )

    async def probe(_daemon, sid):
        if probe_error:
            raise probe_error
        machine.note(sid, TAB_RECOVERED, executor_alive=True)

    monkeypatch.setattr(
        "browserwright.daemon.server.exec_relay.probe_executor_binding", probe)
    await router._handle_recover(client, {"session": "7"}, 41)
    return replies[-1]["result"], relay, extension, registry, machine


@pytest.mark.asyncio
async def test_recover_keeps_a_live_executor_and_reports_healthy(monkeypatch):
    result, relay, extension, registry, machine = await _invoke_recover(monkeypatch)

    assert result["state"] == HEALTHY
    assert result["steps"] == []
    assert extension.calls == []
    assert registry.ensure_calls == []
    assert machine.state_of("7") == HEALTHY
    assert relay.wait_calls == 0


@pytest.mark.asyncio
async def test_recover_extension_timeout_is_bounded_needs_human(monkeypatch):
    result, relay, extension, registry, _ = await _invoke_recover(
        monkeypatch, ready=False, executor_alive=False)

    assert result["state"] == NEEDS_HUMAN
    assert len(result["steps"]) == 1
    assert result["steps"][0]["rung"] == "extension"
    assert result["steps"][0]["ok"] is False
    assert relay.wait_calls == 1
    assert extension.calls == []
    assert registry.ensure_calls == []


@pytest.mark.asyncio
async def test_recover_cold_starts_only_the_requested_executor(monkeypatch):
    result, _, _, registry, machine = await _invoke_recover(
        monkeypatch, backend="cdp", executor_alive=False)

    assert registry.ensure_calls == ["7"]
    assert result["state"] == HEALTHY
    assert machine.state_of("7") == HEALTHY
    assert [s["rung"] for s in result["steps"]] == ["executor", "tab"]


@pytest.mark.asyncio
async def test_recover_cdp_binding_failure_is_needs_human(monkeypatch):
    result, _, _, _, machine = await _invoke_recover(
        monkeypatch, backend="cdp", executor_alive=True,
        probe_error=RuntimeError("cannot bind tab"))

    assert result["state"] == NEEDS_HUMAN
    assert result["steps"][-1]["rung"] == "tab"
    assert result["steps"][-1]["ok"] is False
    assert machine.state_of("7") == NEEDS_HUMAN


@pytest.mark.asyncio
async def test_recover_executor_failure_is_needs_human(monkeypatch):
    result, _, _, registry, machine = await _invoke_recover(
        monkeypatch, backend="cdp", executor_alive=False,
        spawn_error=RuntimeError("cannot spawn"))

    assert registry.ensure_calls == ["7"]
    assert result["state"] == NEEDS_HUMAN
    assert result["steps"][-1]["ok"] is False
    assert "cannot spawn" in result["reason"]
    assert machine.state_of("7") == NEEDS_HUMAN


@pytest.mark.asyncio
async def test_no_existing_tab_is_recoverable_not_a_false_success(monkeypatch):
    """Exit 0 means healthy, so recover may not return tab-gone as success."""
    result, _, _, _, _ = await _invoke_recover(
        monkeypatch, tab_error=RuntimeError("no recoverable tabs"),
        executor_alive=True)

    assert result["state"] == HEALTHY


@pytest.mark.parametrize("state", [
    "extension-disconnected", "tab-gone", "executor-unbound", "executor-dead",
    NEEDS_HUMAN,
])
def test_user_cli_exit_zero_means_healthy_only(state, capsys):
    assert user_cli._recover_report("7", state, [], "broken") == 4
    assert state in capsys.readouterr().out


def test_user_cli_healthy_result_exits_zero(monkeypatch, capsys):
    monkeypatch.setattr(launchagent, "daemon_self_check", lambda _cfg: {
        "healthy": True, "criterion": None, "detail": "two good probes",
        "probes": ["ours", "ours"],
    })
    monkeypatch.setattr(
        "browserwright.daemon_url.daemon_endpoint",
        lambda **_kw: DaemonEndpoint("http://127.0.0.1:19990", False, "default"),
    )
    monkeypatch.setattr("subprocess.run", lambda *a, **k: CompletedProcess(
        a[0], 0, json.dumps({"sessionId": "7", "state": HEALTHY,
                            "steps": [], "reason": ""}), ""))

    assert user_cli._cmd_recover(["--session", "7"]) == 0
    assert "healthy" in capsys.readouterr().out


def test_daemon_self_check_requires_two_agreeing_probes(monkeypatch):
    probes = iter([
        EndpointProbe("refused", "127.0.0.1", 19990),
        EndpointProbe("ours", "127.0.0.1", 19990, pid=12, version="1.2.3"),
    ])
    monkeypatch.setattr(
        "browserwright.daemon._ipc.probe_endpoint_sync",
        lambda *_a, **_k: next(probes),
    )
    monkeypatch.setattr(
        "browserwright.daemon.launchagent.daemon_endpoint",
        lambda: DaemonEndpoint("http://127.0.0.1:19990", False, "default"),
        raising=False,
    )

    verdict = launchagent.daemon_self_check(None, expected_version="1.2.3")
    assert verdict["healthy"] is False
    assert verdict["criterion"] is None
    assert verdict["probes"] == ["refused", "ours"]


def test_daemon_self_check_is_healthy_only_for_two_matching_current_versions(
        monkeypatch):
    probe = EndpointProbe(
        "ours", "127.0.0.1", 19990, pid=12, version="1.2.3")
    monkeypatch.setattr(
        "browserwright.daemon._ipc.probe_endpoint_sync",
        lambda *_a, **_k: probe,
    )

    verdict = launchagent.daemon_self_check(None, expected_version="1.2.3")
    assert verdict["healthy"] is True
    assert verdict["criterion"] is None
    assert verdict["probes"] == ["ours", "ours"]


@pytest.mark.parametrize(("probe", "criterion"), [
    (EndpointProbe("refused", "127.0.0.1", 19990), "gone"),
    (EndpointProbe("foreign", "127.0.0.1", 19990, status_line="HTTP/1.1 503"), "foreign"),
    (EndpointProbe("ours", "127.0.0.1", 19990, pid=12, version="old"), "version"),
])
def test_daemon_self_check_classifies_two_matching_failures(
        monkeypatch, probe, criterion):
    monkeypatch.setattr(
        "browserwright.daemon._ipc.probe_endpoint_sync",
        lambda *_a, **_k: probe,
    )
    verdict = launchagent.daemon_self_check(None, expected_version="new")
    assert verdict["healthy"] is False
    assert verdict["criterion"] == criterion
    assert verdict["probes"] == [probe.kind, probe.kind]


def test_daemon_self_check_uses_the_requested_config_port(monkeypatch):
    from browserwright.daemon.config import Config

    seen = []
    probe = EndpointProbe(
        "ours", "127.0.0.1", 32190, pid=12, version="1.2.3")
    monkeypatch.setattr(
        "browserwright.daemon._ipc.probe_endpoint_sync",
        lambda host, port, **_kw: seen.append((host, port)) or probe,
    )
    cfg = Config()
    cfg.facade_host = "100.72.20.32"
    cfg.facade_port = 32190

    verdict = launchagent.daemon_self_check(cfg, expected_version="1.2.3")

    assert verdict["healthy"] is True
    assert seen == [("127.0.0.1", 32190), ("127.0.0.1", 32190)]
