from __future__ import annotations

import argparse
import asyncio
import json
from types import SimpleNamespace

import pytest

from browserwright.daemon import cli, launchagent
from browserwright.daemon.config import Config
from browserwright.daemon.errors import DaemonError, Unavailable, UserError
from browserwright.daemon.probe import DaemonProbe


def _ns(**kwargs):
    return SimpleNamespace(**kwargs)


def _cmd(name):
    """A forwarding subcommand's handler, by subcommand name.

    `open-background` / `close-tab` / `end-session` / `kill-executor` are rows in
    `cli._FORWARDS` rather than hand-written functions, so the dispatch table is
    where you get at them."""
    return cli._DISPATCH[name]


class _AsyncWs:
    def __init__(self, frames=()):
        self.frames = list(frames)
        self.sent = []
        self.closed = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def send(self, payload):
        self.sent.append(json.loads(payload))

    async def recv(self):
        if self.frames:
            return json.dumps(self.frames.pop(0))
        return json.dumps({"method": "BrowserwrightDaemon.event"})

    async def close(self):
        self.closed = True


def test_status_list_and_stop_error_branches(monkeypatch, capsys, tmp_path):
    from browserwright.daemon import _ipc, platforms

    cleaned = []
    monkeypatch.setattr(_ipc, "cleanup_endpoint", lambda: cleaned.append("cleanup"))
    monkeypatch.setattr(_ipc, "ping_sync", lambda timeout: None)
    assert cli._cmd_stop(_ns(timeout=0), Config()) == 0
    assert cleaned == ["cleanup"]

    calls = {"proc": 0}

    def fake_proc_start(pid):
        calls["proc"] += 1
        return 1 if calls["proc"] == 1 else 2

    monkeypatch.setattr(_ipc, "ping_sync", lambda timeout: 2468)
    monkeypatch.setattr(platforms, "proc_start_time", fake_proc_start)
    assert cli._cmd_stop(_ns(timeout=0), Config()) == 0

    # Isolate the control socket to a non-existent path so the issue #15 (2.1)
    # port-held-zombie probe is skipped — otherwise this unit test would pick up
    # a real daemon holding the default relay/facade ports on the dev machine.
    class _DeadProbe(DaemonProbe):
        def ping(self, timeout):
            return (None, None)

        def socket_present(self):
            return False

        def endpoint(self):
            return {"transport": "unix", "path": str(tmp_path / "dead.sock")}

        def facade(self):
            return (None, None)

    assert cli._cmd_status(_ns(json=False), Config(),
                           probe=_DeadProbe(Config())) == 2
    monkeypatch.setattr(launchagent, "plist_path", lambda: tmp_path / "absent.plist")
    monkeypatch.setattr(cli, "_ipc_ping", lambda: None)
    assert cli._cmd_list(_ns(json=False), Config()) == 0

    captured = capsys.readouterr()
    assert "cleaned up stale files" in captured.err
    assert "was recycled by another process" in captured.err
    assert "daemon not running" in captured.out
    assert "no daemon installed or running" in captured.out


def test_backend_info_attach_and_rpc_success_paths(monkeypatch, capsys, tmp_path):
    from browserwright.daemon import _ipc
    import websockets

    real_rpc_via_ws = cli._rpc_via_ws

    async def live_ping(timeout):
        return 999

    monkeypatch.setattr(_ipc, "ping_async", live_ping)
    async def fake_backend_rpc(*args, **kwargs):
        return {
            "name": "extension",
            "kind": "relay",
            "schema_version": 7,
        }

    backend_calls = []

    async def fake_backend_rpc_capture(*args, **kwargs):
        backend_calls.append((args, kwargs))
        return await fake_backend_rpc(*args, **kwargs)

    monkeypatch.setattr(cli, "_rpc_via_ws", fake_backend_rpc_capture)
    assert cli._cmd_backend_info(_ns(json=False, session="bw-s"), Config()) == 0
    assert backend_calls[-1][0][2] == {"bsSession": "bw-s"}
    assert backend_calls[-1][1]["browser_session"] == "bw-s"

    attach_calls = []

    async def fake_attach_rpc(*args, **kwargs):
        attach_calls.append((args, kwargs))
        return {
            "targetId": "T",
            "url": "https://example.test/",
            "title": "Example",
        }

    monkeypatch.setattr(cli, "_rpc_via_ws", fake_attach_rpc)
    assert cli._cmd_attach_active(_ns(json=True, session="bw-s"), Config()) == 0
    assert attach_calls[-1][0][1] == "BrowserwrightDaemon.attachActiveTab"
    assert attach_calls[-1][0][2] == {"bsSession": "bw-s"}
    assert attach_calls[-1][1]["browser_session"] == "bw-s"
    monkeypatch.setattr(cli, "_rpc_via_ws", real_rpc_via_ws)

    monkeypatch.setattr(_ipc, "sock_path", lambda: tmp_path / "daemon.sock")

    rpc_ws = _AsyncWs([
        {"method": "noise"},
        {"id": 1, "result": {"ok": True}},
    ])
    sock = tmp_path / "daemon.sock"
    sock.write_text("")
    monkeypatch.setattr(websockets, "unix_connect", lambda *a, **kw: rpc_ws)
    assert asyncio.run(cli._rpc_via_ws(
        Config(), "BrowserwrightDaemon.test", {"x": 1},
        client_label="sweep", timeout=0.01,
    )) == {"ok": True}
    assert rpc_ws.sent == [
        {"id": 1, "method": "BrowserwrightDaemon.test", "params": {"x": 1}}
    ]

    out = capsys.readouterr().out
    assert '"backend": "extension"' in out
    assert '"targetId": "T"' in out


def test_rpc_attach_error_paths(monkeypatch, capsys, tmp_path):
    from browserwright.daemon import _ipc
    import websockets

    monkeypatch.setattr(_ipc, "sock_path", lambda: tmp_path / "absent.sock")
    assert cli._cmd_attach_active(_ns(json=False, session="bw-s"), Config()) == 2
    with pytest.raises(Unavailable):
        asyncio.run(cli._rpc_via_ws(
            Config(), "BrowserwrightDaemon.missing", {},
            client_label="sweep",
        ))

    sock = tmp_path / "daemon.sock"
    sock.write_text("")
    monkeypatch.setattr(_ipc, "sock_path", lambda: sock)
    monkeypatch.setattr(websockets, "unix_connect", lambda *a, **kw: _AsyncWs([
        {"id": 1, "error": {"message": "boom", "code": -32000}},
    ]))
    with pytest.raises(DaemonError, match="boom"):
        asyncio.run(cli._rpc_via_ws(
            Config(), "BrowserwrightDaemon.fail", {},
            client_label="sweep",
        ))

    monkeypatch.setattr(websockets, "unix_connect", lambda *a, **kw: _AsyncWs([
        {"id": 1, "result": "not-a-dict"},
    ]))
    with pytest.raises(DaemonError, match="non-dict"):
        asyncio.run(cli._rpc_via_ws(
            Config(), "BrowserwrightDaemon.bad", {},
            client_label="sweep",
        ))

    err_ws = _AsyncWs([{"id": 1, "error": {"message": "attach boom"}}])
    monkeypatch.setattr(websockets, "unix_connect", lambda *a, **kw: err_ws)
    assert cli._cmd_attach_active(_ns(json=False, session="bw-s"), Config()) == 1

    captured = capsys.readouterr()
    assert captured.err.count("no daemon running") >= 1
    assert "attach-active failed:" in captured.err
    assert "attach boom" in captured.err


def test_userscript_actions_success_and_error_sweep(monkeypatch, capsys):
    calls = []

    async def fake_call(cfg, method, params, *, session, timeout=5.0):
        assert session == "bw-s"
        calls.append((method, params))
        if method.endswith(".remove"):
            raise UserError("bad key")
        if method.endswith(".toggle"):
            raise Unavailable("offline")
        if method.endswith(".logs"):
            raise DaemonError("log boom")
        return {"ok": True, "method": method, "params": params}

    monkeypatch.setattr(cli, "_userscript_call_ws", fake_call)

    def _us_ns(*argv: str):
        return cli._build_parser().parse_args(["userscript", *argv])

    assert cli._cmd_userscript(_ns(userscript_cmd=None), Config()) == 1
    assert cli._cmd_userscript(_us_ns("--session", "bw-s", "list", "--site", "https://example.test/"), Config()) == 0
    assert cli._cmd_userscript(_us_ns("--session", "bw-s", "remove", "missing"), Config()) == 1
    assert cli._cmd_userscript(_us_ns("--session", "bw-s", "toggle", "id1", "--enabled", "off"), Config()) == 2
    assert cli._cmd_userscript(_us_ns("--session", "bw-s", "logs", "--id", "id1", "--limit", "2"), Config()) == 3

    assert calls == [
        (
            "BrowserwrightDaemon.userscript.list",
            {"site": "https://example.test/"},
        ),
        ("BrowserwrightDaemon.userscript.remove", {"key": "missing"}),
        (
            "BrowserwrightDaemon.userscript.toggle",
            {"key": "id1", "enabled": False},
        ),
        (
            "BrowserwrightDaemon.userscript.logs",
            {"limit": 2, "scriptId": "id1"},
        ),
    ]
    captured = capsys.readouterr()
    assert "usage: browserwright-daemon userscript" in captured.err
    assert "error: bad key" in captured.err
    assert "error: offline" in captured.err
    assert "error: log boom" in captured.err
    assert '"ok": true' in captured.out


def test_userscript_install_stdin_failed_sync_warns(monkeypatch, capsys):
    async def fake_call(cfg, method, params, *, session, timeout=5.0):
        assert session == "bw-s"
        return {
            "id": "remote-id",
            "identity": "remote-ident",
            "sync": {
                "ok": False,
                "reason": "userScripts API unavailable in profile",
                "failed": [{"identity": "n:X", "error": "denied"}],
            },
        }

    monkeypatch.setattr(cli, "_userscript_call_ws", fake_call)
    monkeypatch.setattr(cli.sys, "stdin", _ns(read=lambda: (
        "// ==UserScript==\n"
        "// @name X\n"
        "// @namespace n\n"
        "// @match https://example.test/*\n"
        "// ==/UserScript==\n"
        "window.x = 1;\n"
    )))
    assert cli._cmd_userscript(
        cli._build_parser().parse_args(
            ["userscript", "--session", "bw-s", "install", "-"]),
        Config()) == 2
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert payload["id"] == "remote-id"
    assert payload["sync"]["ok"] is False
    assert "stored but NOT active" in captured.err
    assert "Allow user scripts" in captured.err
    assert "registration failed for n:X: denied" in captured.err


def test_tab_rpc_handlers_cover_success_and_failures(monkeypatch, capsys):
    calls = []
    responses = [
        {"sessionId": "S", "targetId": "T"},
        Unavailable("socket down"),
        {"closed": [1]},
        DaemonError("close boom"),
        {"ok": True, "closed": [], "failed": [], "kept": ["T"]},
        DaemonError("end boom"),
    ]

    def fake_rpc(cfg, method, params, **kwargs):
        calls.append((method, params, kwargs))
        item = responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    def fake_run(value):
        return value

    monkeypatch.setattr(cli, "_rpc_via_ws", fake_rpc)
    monkeypatch.setattr(cli, "_run", fake_run)

    assert _cmd("open-background")(_ns(url="https://example.test/", group="G", session="bw-s"), Config()) == 0
    assert _cmd("open-background")(_ns(url="https://down.test/", group="G", session="bw-s"), Config()) == 2
    assert _cmd("close-tab")(_ns(session="bw-s", session_id="S", target_id="T"), Config()) == 0
    assert _cmd("close-tab")(_ns(session="bw-s", session_id=None, target_id="T2"), Config()) == 3
    assert _cmd("end-session")(_ns(session="sess", group_id=None), Config()) == 0
    assert _cmd("end-session")(_ns(session="sess", group_id=8), Config()) == 3

    assert calls[0] == (
        "BrowserwrightDaemon.openBackgroundTab",
        {"url": "https://example.test/", "groupName": "G", "bsSession": "bw-s"},
        {"client_label": "cli-open-background", "timeout": 15.0,
         "browser_session": "bw-s"},
    )
    assert calls[2][1] == {"bsSession": "bw-s", "sessionId": "S", "targetId": "T"}
    assert calls[4][1] == {"session": "sess"}
    assert calls[5][1] == {"session": "sess"}  # ADR-0009: no group id
    captured = capsys.readouterr()
    assert '"sessionId": "S"' in captured.out
    assert '"kept": ["T"]' in captured.out
    assert "error: socket down" in captured.err
    assert "error: close boom" in captured.err
    assert "error: end boom" in captured.err


def test_end_and_kill_executor_pass_browser_session_for_ws_scoping(monkeypatch):
    """Regression: `end-session` / `kill-executor` MUST put the session on the
    transient ws (`browser_session=`), or the daemon's `_require_browser_session`
    rejects the ws (no `?session=`) at the boundary before the executor reap
    runs — the discovery file then survives `session end` (phase B e2e bug)."""
    calls = []

    def fake_rpc(cfg, method, params, **kwargs):
        calls.append((method, kwargs))
        return (
            {"ok": True, "reaped": True}
            if method == "BrowserwrightDaemon.killExecutor"
            else {"ok": True}
        )

    monkeypatch.setattr(cli, "_rpc_via_ws", fake_rpc)
    monkeypatch.setattr(cli, "_run", lambda value: value)

    assert _cmd("end-session")(_ns(session="bw-s", group_id=None), Config()) == 0
    assert _cmd("kill-executor")(_ns(session="bw-s"), Config()) == 0

    end_method, end_kwargs = calls[0]
    assert end_method == "BrowserwrightDaemon.endSession"
    assert end_kwargs["browser_session"] == "bw-s"

    kill_method, kill_kwargs = calls[1]
    assert kill_method == "BrowserwrightDaemon.killExecutor"
    assert kill_kwargs["browser_session"] == "bw-s"


def test_kill_executor_requires_confirmed_reap(monkeypatch, capsys):
    async def not_reaped(*args, **kwargs):
        return {
            "ok": True,
            "killed": False,
            "reaped": False,
            "matched": True,
        }

    monkeypatch.setattr(cli, "_rpc_via_ws", not_reaped)

    assert _cmd("kill-executor")(_ns(session="bw-s"), Config()) == 3
    streams = capsys.readouterr()
    assert streams.out == ""
    assert "did not confirm executor death" in streams.err


def test_end_session_initiate_then_join_polls_to_the_final_result(
    monkeypatch, capsys,
):
    """Issue #32: `end-session` initiates, then re-issues `endSession` to
    join the daemon-side teardown, printing progress while it waits. Exit 0
    only with the FINAL result."""
    calls = []
    responses = [
        {"ok": True, "initiated": True, "phase": "terminating",
         "backend": "extension"},
        {"ok": True, "closed": [1, 2, 3], "failed": [], "unknown": [],
         "kept": [], "backend": "extension"},
    ]

    def fake_rpc(cfg, method, params, **kwargs):
        calls.append((method, params, kwargs))
        return responses.pop(0)

    monkeypatch.setattr(cli, "_rpc_via_ws", fake_rpc)
    monkeypatch.setattr(cli, "_run", lambda value: value)

    assert _cmd("end-session")(_ns(session="bw-s", group_id=7), Config()) == 0

    assert len(calls) == 2
    assert calls[0][1] == {"session": "bw-s"}  # ADR-0009: no group id
    for _method, _params, kwargs in calls:
        assert kwargs["browser_session"] == "bw-s"
        assert kwargs["client_label"] == "cli-end-session"
    streams = capsys.readouterr()
    assert "tearing down" in streams.err
    assert '"closed": [1, 2, 3]' in streams.out


def test_end_session_old_daemon_final_result_needs_no_poll(
    monkeypatch, capsys,
):
    """Against a daemon without the initiate contract the first response is
    already the final result: no poll loop, no progress output."""
    calls = []

    def fake_rpc(cfg, method, params, **kwargs):
        calls.append(method)
        return {"ok": True, "closed": []}

    monkeypatch.setattr(cli, "_rpc_via_ws", fake_rpc)
    monkeypatch.setattr(cli, "_run", lambda value: value)

    assert _cmd("end-session")(_ns(session="bw-s", group_id=None), Config()) == 0
    assert calls == ["BrowserwrightDaemon.endSession"]
    streams = capsys.readouterr()
    assert "tearing down" not in streams.err
    assert '"closed": []' in streams.out


def test_end_session_join_timeout_keeps_polling(monkeypatch, capsys):
    """A join whose RPC times out (the daemon is still tearing down) is not a
    failure: the CLI reports progress and re-issues the join."""
    calls = []
    responses = [
        {"ok": True, "initiated": True, "phase": "terminating",
         "backend": "extension"},
        TimeoutError("join timed out"),
        {"ok": True, "closed": [1], "failed": [], "unknown": [],
         "kept": [], "backend": "extension"},
    ]

    def fake_rpc(cfg, method, params, **kwargs):
        calls.append(kwargs.get("timeout"))
        item = responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    monkeypatch.setattr(cli, "_rpc_via_ws", fake_rpc)
    monkeypatch.setattr(cli, "_run", lambda value: value)

    assert _cmd("end-session")(_ns(session="bw-s", group_id=None), Config()) == 0
    assert calls == [cli._END_SESSION_INITIATE_TIMEOUT,
                     cli._END_SESSION_JOIN_TIMEOUT,
                     cli._END_SESSION_JOIN_TIMEOUT]
    assert "tearing down" in capsys.readouterr().err


def test_end_session_still_terminating_hits_the_wait_cap(monkeypatch, capsys):
    """A teardown that never completes must fail loudly — after visible
    progress — instead of hanging the user silently."""
    def fake_rpc(cfg, method, params, **kwargs):
        return {"ok": True, "initiated": True, "phase": "terminating",
                "backend": "extension"}

    monkeypatch.setattr(cli, "_rpc_via_ws", fake_rpc)
    monkeypatch.setattr(cli, "_run", lambda value: value)
    monkeypatch.setattr(cli, "_END_SESSION_TOTAL_WAIT_S", 0.05)
    monkeypatch.setattr(cli, "_END_SESSION_JOIN_TIMEOUT", 0.01)

    assert _cmd("end-session")(_ns(session="bw-s", group_id=None), Config()) == 3
    streams = capsys.readouterr()
    assert "still terminating" in streams.err
    assert "browserwright-daemon ps" in streams.err
    assert streams.out == ""


def test_end_session_partial_join_is_an_error(monkeypatch, capsys):
    """The join's final result is validated like any endSession result: a
    partial workspace teardown is an honest failure, never a silent 0."""
    responses = [
        {"ok": True, "initiated": True, "phase": "terminating",
         "backend": "extension"},
        {"ok": False, "partial": True, "closed": [1],
         "failed": ["workspace"], "unknown": [], "kept": []},
    ]

    def fake_rpc(cfg, method, params, **kwargs):
        return responses.pop(0)

    monkeypatch.setattr(cli, "_rpc_via_ws", fake_rpc)
    monkeypatch.setattr(cli, "_run", lambda value: value)

    assert _cmd("end-session")(_ns(session="bw-s", group_id=None), Config()) == 3
    streams = capsys.readouterr()
    assert "left tabs open" in streams.err
    assert streams.out == ""


def test_launchagent_install_uninstall_and_launchctl_sweep(monkeypatch, capsys, tmp_path):
    import subprocess

    plist = tmp_path / "Library" / "LaunchAgents" / "com.browserwright-daemon.plist"
    launchctl_calls = []

    with monkeypatch.context() as m:
        m.setattr(subprocess, "run", lambda *a, **kw: (_ for _ in ()).throw(FileNotFoundError("missing")))
        assert launchagent.launchctl("load", "x") == (-1, "", "missing")

    monkeypatch.setattr(launchagent.sys, "platform", "darwin")
    monkeypatch.setattr(launchagent, "plist_path", lambda: plist)
    monkeypatch.setattr(launchagent, "resolve_daemon_bin", lambda: "/bin/browserwright-daemon")
    monkeypatch.setattr(launchagent.os.path, "expanduser", lambda p: str(tmp_path / "home" / p.removeprefix("~/")))
    monkeypatch.setattr(launchagent, "launchctl", lambda *args: launchctl_calls.append(args) or (0, "", ""))

    assert cli._cmd_install(_ns(backend=None, extension_port=29999, force=False), Config()) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
    assert payload["extension_port"] == 29999
    assert launchctl_calls == [("load", "-w", str(plist))]
    assert "--backend" not in plist.read_text()
    assert "<string>serve</string>" in plist.read_text()

    plist.unlink()
    assert cli._cmd_install(_ns(backend="cdp", extension_port=None, force=False), Config()) == 0
    assert "--backend" not in plist.read_text()
    assert "install --backend` is ignored" in capsys.readouterr().err

    assert cli._cmd_install(_ns(backend=None, extension_port=None, force=False), Config()) == 1
    assert "already exists" in capsys.readouterr().err

    assert cli._cmd_uninstall(_ns(), Config()) == 0
    assert json.loads(capsys.readouterr().out)["removed"] == str(plist)

    assert cli._cmd_uninstall(_ns(), Config()) == 0
    assert json.loads(capsys.readouterr().out)["reason"] == "no LaunchAgent installed"

    monkeypatch.setattr(launchagent, "launchctl", lambda *args: (9, "", "load failed"))
    assert cli._cmd_install(_ns(backend=None, extension_port=None, force=True), Config()) == 3
    assert not plist.exists()
    assert "launchctl load failed: load failed" in capsys.readouterr().err


def test_every_declared_subcommand_has_a_handler_and_vice_versa():
    """The failure mode a dispatch table invites: add a parser subcommand and
    forget the row (argparse accepts it, `main` prints help and exits 1), or add
    a row and forget the parser (dead code nobody can reach). Lock both."""
    parser = cli._build_parser()
    declared = set(
        next(a for a in parser._actions
             if isinstance(a, argparse._SubParsersAction)).choices)

    assert declared == set(cli._DISPATCH), (
        "parser subcommands and _DISPATCH disagree: "
        f"parser-only={sorted(declared - set(cli._DISPATCH))} "
        f"dispatch-only={sorted(set(cli._DISPATCH) - declared)}")
    assert set(cli._FORWARDS) <= declared


def test_forwarded_methods_are_namespaced_daemon_verbs():
    """Every forwarding row must target a `BrowserwrightDaemon.*` verb — a bare
    CDP method here would be forwarded upstream instead of answered by the
    daemon, which is a silently different (and wrong) code path."""
    for name, spec in cli._FORWARDS.items():
        assert spec.method.startswith("BrowserwrightDaemon."), (name, spec.method)
        assert spec.label.startswith("cli-"), (name, spec.label)
