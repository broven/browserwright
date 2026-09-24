"""Focused unit contracts for ADR-0013's explicit recovery path."""
from __future__ import annotations

import asyncio
import json
import os
from subprocess import CompletedProcess
from types import SimpleNamespace

import pytest

from browserwright import cli as user_cli
from browserwright import session_registry
from browserwright.daemon import cli as daemon_cli
from browserwright.daemon import launchagent
from browserwright.daemon._ipc import EndpointProbe
from browserwright.daemon.config import Config
from browserwright.daemon.server import executor_registry as registry_mod
from browserwright.daemon.server.daemon import Daemon
from browserwright.daemon.server.relay import RelayServer
from browserwright.daemon.server.session_state import (
    HEALTHY,
    NEEDS_HUMAN,
    TAB_RECOVERED,
)
from browserwright.daemon.server.upstream import CdpUpstream
from browserwright.daemon.server.upstream_context import build_context
from browserwright.daemon_url import DaemonEndpoint


class _Browser:
    """The browser side of the daemon: the only thing stood in for.

    Everything between the verb and these round-trips — the Router, the
    ``Daemon`` and its drivable path, the context factory, the adapters, the
    executor registry and the recovery state machine — is the real one.
    """

    def __init__(self, *, ready: bool, tab_error: Exception | None):
        self.ready = ready
        self.tab_error = tab_error
        self.wait_calls = 0
        self.calls: list = []

    async def wait_ready(self, timeout):
        self.wait_calls += 1
        if not self.ready:
            raise asyncio.TimeoutError

    async def recover_session(self, sid):
        self.calls.append(sid)
        if self.tab_error:
            raise self.tab_error
        return {"sessionId": "u1", "targetId": "ext-tab-1", "recovered": [1]}

    async def open_background_tab(self, url, *, session_id, background):
        self.calls.append(("open", url, session_id, background))
        return {"sessionId": "u2", "targetId": "ext-tab-2", "tabId": 2}


async def _invoke_recover(monkeypatch, tmp_path, *, backend="extension",
                          ready=True, tab_error=None, executor_alive=True,
                          spawn_error=None, probe_error=None,
                          initial_state=None):
    monkeypatch.setenv("BS_HOME", str(tmp_path))
    sid = session_registry.allocate(backend=backend, owner="attach", name="t")

    browser = _Browser(ready=ready, tab_error=tab_error)
    monkeypatch.setattr(RelayServer, "is_ready",
                        property(lambda _self: browser.ready))
    monkeypatch.setattr(RelayServer, "wait_ready",
                        lambda _relay, timeout: browser.wait_ready(timeout))

    async def _no_browser(self, ws_url=None, *, timeout=None):
        return None

    async def _current_page(self, session_id=None):
        return {"sessionId": "u3", "targetId": "T1", "tabId": None}

    monkeypatch.setattr(CdpUpstream, "open", _no_browser)
    monkeypatch.setattr(CdpUpstream, "current_page", _current_page)

    cfg = Config()
    daemon = Daemon(cfg=cfg, shared_context=build_context(
        backend="extension", cfg=cfg))
    ext = daemon.shared_context.upstream
    ext.recover_session = browser.recover_session
    ext.open_background_tab = browser.open_background_tab

    spawned: list[str] = []

    async def _spawn(session_id):
        # The executor subprocess is the other stand-in: a handle whose
        # liveness is this test process.
        spawned.append(session_id)
        if spawn_error:
            raise spawn_error
        return registry_mod.ExecutorHandle(
            session_id=session_id, proc=None, sock_path=f"/tmp/{session_id}.s",
            pid=os.getpid())

    monkeypatch.setattr(daemon.executors, "_spawn", _spawn)
    if executor_alive:
        await daemon.executors.ensure(sid)
        spawned.clear()
    if initial_state is not None:
        session_registry.update(
            sid, recovery={"state": initial_state, "since": 1.0})
    # Boot: the state machine is rebuilt from the ledger (ADR-0013 rule 1).
    daemon.recovery.load(session_registry.list_all(), extension_connected=ready,
                         executor_alive=daemon.executor_alive)

    async def probe(_daemon, session_id):
        if probe_error:
            raise probe_error
        daemon.recovery.note(session_id, TAB_RECOVERED, executor_alive=True)

    monkeypatch.setattr(
        "browserwright.daemon.server.exec_relay.probe_executor_binding", probe)

    ctx = daemon.context_for_required(sid)
    client = ctx.state.allocate_client("test")
    client.session_id = sid
    replies = []

    async def send(text):
        replies.append(json.loads(text))

    ctx.router.register_client(client.client_id, send)
    await ctx.router.route_from_client(client, json.dumps({
        "id": 41, "method": "BrowserwrightDaemon.recover",
        "params": {"session": sid}}))
    result = replies[-1]["result"]
    return result, browser, spawned, daemon.recovery.state_of(sid)


@pytest.mark.asyncio
async def test_recover_keeps_a_live_executor_and_reports_healthy(
        monkeypatch, tmp_path):
    result, browser, spawned, state = await _invoke_recover(
        monkeypatch, tmp_path, initial_state=HEALTHY)

    assert result["state"] == HEALTHY
    assert result["steps"] == []
    assert browser.calls == []
    assert spawned == []
    assert state == HEALTHY
    assert browser.wait_calls == 0


@pytest.mark.asyncio
async def test_recover_extension_timeout_is_bounded_needs_human(
        monkeypatch, tmp_path):
    result, browser, spawned, _ = await _invoke_recover(
        monkeypatch, tmp_path, ready=False, executor_alive=False)

    assert result["state"] == NEEDS_HUMAN
    assert len(result["steps"]) == 1
    assert result["steps"][0]["rung"] == "browser"
    assert result["steps"][0]["ok"] is False
    assert "extension is not connected" in result["reason"]
    assert browser.wait_calls == 1
    assert browser.calls == []
    assert spawned == []


@pytest.mark.asyncio
async def test_recover_cold_starts_only_the_requested_executor(
        monkeypatch, tmp_path):
    result, _, spawned, state = await _invoke_recover(
        monkeypatch, tmp_path, backend="cdp", executor_alive=False)

    assert len(spawned) == 1
    assert result["state"] == HEALTHY
    assert state == HEALTHY
    assert [s["rung"] for s in result["steps"]] == ["executor"]


@pytest.mark.asyncio
async def test_recover_cdp_binding_failure_is_needs_human(
        monkeypatch, tmp_path):
    """A resident executor under a replacement daemon: the cdp tab is not
    re-proven until the executor's own binding answers."""
    result, _, _, state = await _invoke_recover(
        monkeypatch, tmp_path, backend="cdp", executor_alive=True,
        probe_error=RuntimeError("cannot bind tab"))

    assert result["state"] == NEEDS_HUMAN
    assert [s["rung"] for s in result["steps"]] == [
        "tab", "executor", "binding"]
    assert result["steps"][-1]["ok"] is False
    assert state == NEEDS_HUMAN


@pytest.mark.asyncio
async def test_recover_executor_failure_is_needs_human(monkeypatch, tmp_path):
    result, _, spawned, state = await _invoke_recover(
        monkeypatch, tmp_path, backend="cdp", executor_alive=False,
        spawn_error=RuntimeError("cannot spawn"))

    assert len(spawned) == 1
    assert result["state"] == NEEDS_HUMAN
    assert result["steps"][-1]["ok"] is False
    assert "cannot spawn" in result["reason"]
    assert state == NEEDS_HUMAN


@pytest.mark.asyncio
async def test_no_existing_tab_is_recoverable_not_a_false_success(
        monkeypatch, tmp_path):
    """A missing tab becomes a fresh blank tab before recover says healthy."""
    result, browser, _, _ = await _invoke_recover(
        monkeypatch, tmp_path, tab_error=RuntimeError("no recoverable tabs"),
        executor_alive=True, initial_state="tab-gone")

    assert result["state"] == HEALTHY
    assert result["steps"] == [{
        "rung": "tab", "ok": True, "detail": "session has a live tab"}]
    assert browser.calls[1:] == [("open", "about:blank", browser.calls[0], True)]


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


def test_user_recover_parses_needs_human_json_despite_semantic_exit_four(
        monkeypatch, capsys):
    monkeypatch.setattr(launchagent, "daemon_self_check", lambda _cfg: {
        "healthy": True, "criterion": None, "detail": "two good probes",
        "probes": ["ours", "ours"],
    })
    monkeypatch.setattr(
        "browserwright.daemon_url.daemon_endpoint",
        lambda **_kw: DaemonEndpoint("http://127.0.0.1:19990", False, "default"),
    )
    monkeypatch.setattr("subprocess.run", lambda *a, **k: CompletedProcess(
        a[0], 4, json.dumps({
            "sessionId": "7",
            "state": NEEDS_HUMAN,
            "steps": [{"rung": "tab", "ok": False,
                       "detail": "no recoverable tab"}],
            "reason": "the tab cannot be recovered",
        }), "semantic exit 4"))

    assert user_cli._cmd_recover(["--session", "7"]) == 4
    captured = capsys.readouterr()
    assert "session 7: needs-human — the tab cannot be recovered" in captured.out
    assert "tab: no recoverable tab" in captured.err
    assert "semantic exit 4" not in captured.out + captured.err


@pytest.mark.parametrize(("state", "expected_exit"), [
    (HEALTHY, 0),
    (NEEDS_HUMAN, 4),
])
def test_daemon_recover_exit_code_matches_final_state(
        monkeypatch, capsys, state, expected_exit):
    async def recover_result(*_args, **_kwargs):
        return {"sessionId": "7", "state": state, "steps": [], "reason": ""}

    monkeypatch.setattr(daemon_cli, "_rpc_via_ws", recover_result)
    args = SimpleNamespace(session="7")

    assert daemon_cli._DISPATCH["recover"](args, Config()) == expected_exit
    assert json.loads(capsys.readouterr().out)["state"] == state


def test_user_recover_does_not_replace_an_unknown_port_holder(
        monkeypatch, capsys):
    monkeypatch.setattr(launchagent, "daemon_self_check", lambda _cfg: {
        "healthy": False, "criterion": "foreign",
        "detail": "HTTP 503 answered on the daemon port",
        "probes": ["foreign", "foreign"],
    })
    monkeypatch.setattr(
        "browserwright.daemon_url.daemon_endpoint",
        lambda **_kw: DaemonEndpoint("http://127.0.0.1:19990", False, "default"),
    )
    monkeypatch.setattr(
        "browserwright.session_create._ensure_daemon_running",
        lambda: pytest.fail("recover must not disturb an unknown process"),
    )
    monkeypatch.setattr(
        "browserwright.daemon._ipc.log_lifecycle",
        lambda *_args, **_kwargs: pytest.fail("no replacement was attempted"),
    )

    assert user_cli._cmd_recover(["--session", "7"]) == 4
    output = capsys.readouterr().out
    assert "needs-human" in output
    assert "HTTP 503" in output


@pytest.mark.parametrize("criterion", ["gone", "version"])
def test_user_recover_repairs_a_proven_daemon_problem_and_logs_evidence(
        monkeypatch, capsys, criterion):
    from browserwright.daemon import _ipc

    monkeypatch.setattr(launchagent, "daemon_self_check", lambda _cfg: {
        "healthy": False, "criterion": criterion,
        "detail": f"confirmed {criterion}",
        "probes": [criterion, criterion],
    })
    monkeypatch.setattr(
        "browserwright.daemon_url.daemon_endpoint",
        lambda **_kw: DaemonEndpoint("http://127.0.0.1:19990", False, "default"),
    )
    starts = []
    monkeypatch.setattr(
        "browserwright.session_create._ensure_daemon_running",
        lambda: starts.append(criterion),
    )
    monkeypatch.setattr(
        "browserwright.mode_b_client.ModeBClient.wait_until_alive",
        lambda self, timeout: True,
    )
    lifecycle = []
    monkeypatch.setattr(
        _ipc, "log_lifecycle",
        lambda event, **fields: lifecycle.append((event, fields)),
    )
    monkeypatch.setattr("subprocess.run", lambda *a, **k: CompletedProcess(
        a[0], 0, json.dumps({"sessionId": "7", "state": HEALTHY,
                            "steps": [], "reason": ""}), ""))

    assert user_cli._cmd_recover(["--session", "7"]) == 0
    assert starts == [criterion]
    assert lifecycle == [("automatic-recovery", {
        "criterion": criterion,
        "probes": f"{criterion},{criterion}",
        "reason": f"confirmed {criterion}",
        "session": "7",
    })]
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
