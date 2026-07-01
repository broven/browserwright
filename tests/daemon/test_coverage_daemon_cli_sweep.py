from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from browserwright.daemon import cli
from browserwright.daemon.config import Config
from browserwright.daemon.errors import DaemonError, Unavailable, UserError


def _ns(**kwargs):
    return SimpleNamespace(**kwargs)


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


def test_main_parses_config_and_dispatches_core_commands(monkeypatch, capsys):
    seen = []

    def fake_load(**kwargs):
        seen.append(("load", kwargs))
        cfg = Config()
        cfg.backend = kwargs["cli_backend"]
        cfg.extension_port = kwargs["cli_extension_port"]
        return cfg

    def fake_handler(args, cfg):
        seen.append(("handler", args.cmd, cfg.backend, cfg.extension_port))
        print(f"{args.cmd}:{cfg.backend}:{cfg.extension_port}")
        return 17

    monkeypatch.setattr(cli, "load", fake_load)
    monkeypatch.setitem(cli._DISPATCH, "serve", fake_handler)
    rc = cli.main([
        "serve",
        "--backend",
        "extension",
        "--timeout",
        "9",
        "--port",
        "9333",
        "--config",
        "daemon.toml",
        "--extension-port",
        "29999",
    ])
    assert rc == 17
    assert seen == [
        (
            "load",
            {
                "cli_backend": "extension",
                "cli_timeout": 9.0,
                "cli_port": 9333,
                "cli_chrome_binary": None,
                "cli_config_path": "daemon.toml",
                "cli_extension_port": 29999,
                "cli_facade_port": None,
            },
        ),
        ("handler", "serve", "extension", 29999),
    ]
    assert capsys.readouterr().out == "serve:extension:29999\n"


def test_offline_static_handlers_cover_output_modes(monkeypatch, capsys, tmp_path):
    import browserwright.daemon.doctor as doctor_mod
    from browserwright.daemon import _ipc
    from browserwright.daemon.backends.base import ResolveResult

    async def fake_doctor(cfg, backend=None, probe_ws=False):
        return {
            "schema_version": 2,
            "recommended": "env",
            "backends": [
                {
                    "name": backend or "env",
                    "available": True,
                    "ws_url": "ws://env",
                    "detail": "ready",
                    "ux_warning": "",
                    "needs_user_action": "",
                    "ux_cost": "low",
                }
            ],
        }

    async def fake_list_backends(cfg):
        return {
            "schema_version": 2,
            "backends": [
                {
                    "name": "env",
                    "kind": "direct",
                    "recommended_mode": "url",
                    "ux_cost": "low",
                }
            ],
        }

    async def fake_resolve(cfg):
        return ResolveResult("ws://resolved", "env", {"source": "test"})

    monkeypatch.setattr(doctor_mod, "doctor", fake_doctor)
    monkeypatch.setattr(doctor_mod, "list_backends", fake_list_backends)
    monkeypatch.setattr("browserwright.daemon.resolver.resolve", fake_resolve)
    monkeypatch.setattr(_ipc, "endpoint_describe", lambda: {
        "transport": "tcp",
        "path": None,
        "host": "127.0.0.1",
        "port": 4444,
        "token": "tok",
    })
    monkeypatch.setattr(_ipc, "ping_status_sync", lambda timeout: (4321, "v.test"))
    monkeypatch.setattr(_ipc, "log_path", lambda: tmp_path / "missing.log")
    monkeypatch.setattr(cli, "_launchagent_plist_path", lambda: tmp_path / "agent.plist")
    monkeypatch.setattr(cli, "_ipc_ping", lambda: 4321)

    assert cli._cmd_doctor(_ns(json=False, backend="env", probe_ws=True), Config()) == 0
    assert cli._cmd_list_backends(_ns(json=False), Config()) == 0
    assert cli._cmd_url(_ns(mode_b_proxy=False, json=True), Config()) == 0
    assert cli._cmd_url(_ns(mode_b_proxy=True, json=False), Config()) == 0
    assert cli._cmd_status(_ns(json=False), Config()) == 0
    assert cli._cmd_logs(_ns(follow=False), Config()) == 0
    assert cli._cmd_logs(_ns(follow=True), Config()) == 2
    assert cli._cmd_list(_ns(json=False), Config()) == 0
    assert cli._cmd_list(_ns(json=True), Config()) == 0

    captured = capsys.readouterr()
    assert "recommended: env" in captured.out
    assert "env          kind=direct" in captured.out
    assert '"ws_url": "ws://resolved"' in captured.out
    assert "127.0.0.1:4444 token=tok" in captured.out
    assert "daemon alive (pid 4321)" in captured.out
    assert str(tmp_path / "missing.log") in captured.out
    assert '"running": true' in captured.out
    assert "no log file" in captured.err


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

    monkeypatch.setattr(_ipc, "ping_status_sync", lambda timeout: (None, None))
    # Isolate the control socket to a non-existent path so the issue #15 (2.1)
    # port-held-zombie probe is skipped — otherwise this unit test would pick up
    # a real daemon holding the default relay/facade ports on the dev machine.
    monkeypatch.setattr(_ipc, "sock_path", lambda: tmp_path / "dead.sock")
    monkeypatch.setattr(_ipc, "endpoint_describe", lambda: {
        "transport": "unix",
        "path": str(tmp_path / "dead.sock"),
        "host": None,
        "port": None,
        "token": None,
    })
    assert cli._cmd_status(_ns(json=False), Config()) == 2
    monkeypatch.setattr(cli, "_launchagent_plist_path", lambda: tmp_path / "absent.plist")
    monkeypatch.setattr(cli, "_ipc_ping", lambda: None)
    assert cli._cmd_list(_ns(json=False), Config()) == 0

    captured = capsys.readouterr()
    assert "cleaned up stale files" in captured.err
    assert "was recycled by another process" in captured.err
    assert "daemon not running" in captured.out
    assert "no daemon installed or running" in captured.out


def test_backend_info_stats_attach_and_rpc_success_paths(monkeypatch, capsys, tmp_path):
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
    monkeypatch.setattr(cli, "_rpc_via_ws", real_rpc_via_ws)

    stats_ws = _AsyncWs([
        {"method": "noise"},
        {"id": 1, "result": {"clients": 2, "sessions": 3}},
    ])

    async def fake_unix_connect(*args, **kwargs):
        return stats_ws

    monkeypatch.setattr(_ipc, "IS_WINDOWS", False)
    monkeypatch.setattr(_ipc, "sock_path", lambda: tmp_path / "daemon.sock")
    monkeypatch.setattr(websockets, "unix_connect", fake_unix_connect)
    assert cli._cmd_stats(_ns(json=False), Config()) == 0

    attach_json = _AsyncWs([
        {"id": 0, "method": "BrowserwrightDaemon.upstreamReady"},
        {
            "id": 1,
            "result": {
                "targetId": "T",
                "url": "https://example.test/",
                "title": "Example",
            },
        },
    ])
    assert asyncio.run(cli._attach_active_roundtrip(attach_json, _ns(json=True))) == 0

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
    assert "clients\t2" in out
    assert '"targetId": "T"' in out


def test_rpc_disconnect_attach_error_paths(monkeypatch, capsys, tmp_path):
    from browserwright.daemon import _ipc
    import websockets

    monkeypatch.setattr(_ipc, "IS_WINDOWS", False)
    monkeypatch.setattr(_ipc, "sock_path", lambda: tmp_path / "absent.sock")
    assert asyncio.run(cli._disconnect_via_ws(Config(), "because", "bw-s")) == 2
    assert asyncio.run(cli._attach_active_via_ws(Config(), _ns(json=False))) == 2
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
    assert asyncio.run(cli._attach_active_roundtrip(err_ws, _ns(json=False))) == 1

    captured = capsys.readouterr()
    assert captured.err.count("no daemon running") >= 2
    assert "attach-active error: attach boom" in captured.err


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

    assert cli._cmd_userscript(_ns(userscript_cmd=None), Config()) == 1
    assert cli._cmd_userscript(["--session", "bw-s", "list", "--site", "https://example.test/"], Config()) == 0
    assert cli._cmd_userscript(["--session", "bw-s", "remove", "missing"], Config()) == 1
    assert cli._cmd_userscript(["--session", "bw-s", "toggle", "id1", "--enabled", "off"], Config()) == 2
    assert cli._cmd_userscript(["--session", "bw-s", "logs", "--id", "id1", "--limit", "2"], Config()) == 3

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
    assert cli._cmd_userscript(["--session", "bw-s", "install", "-"], Config()) == 2
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
        {"closed": [], "kept": ["T"]},
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

    assert cli._cmd_open_background(_ns(url="https://example.test/", group="G", session="bw-s"), Config()) == 0
    assert cli._cmd_open_background(_ns(url="https://down.test/", group="G", session="bw-s"), Config()) == 2
    assert cli._cmd_close_tab(_ns(session="bw-s", session_id="S", target_id="T"), Config()) == 0
    assert cli._cmd_close_tab(_ns(session="bw-s", session_id=None, target_id="T2"), Config()) == 3
    assert cli._cmd_end_session(_ns(session="sess", group_id=None), Config()) == 0
    assert cli._cmd_end_session(_ns(session="sess", group_id=8), Config()) == 3

    assert calls[0] == (
        "BrowserwrightDaemon.openBackgroundTab",
        {"url": "https://example.test/", "groupName": "G", "bsSession": "bw-s"},
        {"client_label": "cli-open-background", "timeout": 15.0,
         "browser_session": "bw-s"},
    )
    assert calls[2][1] == {"bsSession": "bw-s", "sessionId": "S", "targetId": "T"}
    assert calls[4][1] == {"session": "sess"}
    assert calls[5][1] == {"session": "sess", "groupId": 8}
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
        return {"ok": True}

    monkeypatch.setattr(cli, "_rpc_via_ws", fake_rpc)
    monkeypatch.setattr(cli, "_run", lambda value: value)

    assert cli._cmd_end_session(_ns(session="bw-s", group_id=None), Config()) == 0
    assert cli._cmd_kill_executor(_ns(session="bw-s"), Config()) == 0

    end_method, end_kwargs = calls[0]
    assert end_method == "BrowserwrightDaemon.endSession"
    assert end_kwargs["browser_session"] == "bw-s"

    kill_method, kill_kwargs = calls[1]
    assert kill_method == "BrowserwrightDaemon.killExecutor"
    assert kill_kwargs["browser_session"] == "bw-s"


def test_launchagent_install_uninstall_and_launchctl_sweep(monkeypatch, capsys, tmp_path):
    import subprocess

    plist = tmp_path / "Library" / "LaunchAgents" / "com.browserwright-daemon.plist"
    launchctl_calls = []

    with monkeypatch.context() as m:
        m.setattr(subprocess, "run", lambda *a, **kw: (_ for _ in ()).throw(FileNotFoundError("missing")))
        assert cli._launchctl("load", "x") == (-1, "", "missing")

    monkeypatch.setattr(cli.sys, "platform", "darwin")
    monkeypatch.setattr(cli, "_launchagent_plist_path", lambda: plist)
    monkeypatch.setattr(cli, "_resolve_browserwright_daemon_bin", lambda: "/bin/browserwright-daemon")
    monkeypatch.setattr(cli.os.path, "expanduser", lambda p: str(tmp_path / "home" / p.removeprefix("~/")))
    monkeypatch.setattr(cli, "_launchctl", lambda *args: launchctl_calls.append(args) or (0, "", ""))

    assert cli._cmd_install(_ns(backend=None, extension_port=29999, force=False), Config()) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
    assert payload["extension_port"] == 29999
    assert launchctl_calls == [("load", "-w", str(plist))]
    assert "--backend" not in plist.read_text()
    assert "<string>serve</string>" in plist.read_text()

    plist.unlink()
    assert cli._cmd_install(_ns(backend="rdp", extension_port=None, force=False), Config()) == 0
    assert "--backend" not in plist.read_text()
    assert "install --backend` is ignored" in capsys.readouterr().err

    assert cli._cmd_install(_ns(backend=None, extension_port=None, force=False), Config()) == 1
    assert "already exists" in capsys.readouterr().err

    assert cli._cmd_uninstall(_ns(), Config()) == 0
    assert json.loads(capsys.readouterr().out)["removed"] == str(plist)

    assert cli._cmd_uninstall(_ns(), Config()) == 0
    assert json.loads(capsys.readouterr().out)["reason"] == "no LaunchAgent installed"

    monkeypatch.setattr(cli, "_launchctl", lambda *args: (9, "", "load failed"))
    assert cli._cmd_install(_ns(backend=None, extension_port=None, force=True), Config()) == 3
    assert not plist.exists()
    assert "launchctl load failed: load failed" in capsys.readouterr().err
