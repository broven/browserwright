from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from browserwright.daemon import cli as cli_mod
from browserwright.daemon import platforms as platforms_mod
from browserwright.daemon.backends.base import ResolveResult
from browserwright.daemon.config import Config, load
from browserwright.daemon.errors import (
    Unavailable,
)


@pytest.mark.parametrize(
    ("exc", "expected_code", "expected_stderr"),
    [
        (KeyboardInterrupt(), 130, ""),
        (RuntimeError("surprise"), 3, "internal error: RuntimeError: surprise"),
    ],
)
def test_cli_main_maps_exceptions_to_exit_codes(
    monkeypatch, capsys, exc, expected_code, expected_stderr
):
    def boom(args, cfg):
        raise exc

    monkeypatch.setitem(cli_mod._DISPATCH, "version", boom)
    assert cli_mod.main(["version"]) == expected_code
    captured = capsys.readouterr()
    assert captured.out == ""
    assert expected_stderr in captured.err


def test_cli_main_unavailable_verbose_prints_attempts(monkeypatch, capsys):
    def boom(args, cfg):
        raise Unavailable("nothing resolved", attempts={"env": "unset", "rdp": "closed"})

    monkeypatch.setitem(cli_mod._DISPATCH, "doctor", boom)
    assert cli_mod.main(["doctor", "--verbose"]) == 2
    captured = capsys.readouterr()
    assert "error: nothing resolved" in captured.err
    assert "env: unset" in captured.err
    assert "rdp: closed" in captured.err


def test_cmd_status_json_includes_dead_endpoint(monkeypatch, tmp_path, capsys):
    from browserwright.daemon import _ipc

    monkeypatch.setattr(_ipc, "ping_status_sync", lambda timeout: (None, None))
    monkeypatch.setattr(
        _ipc,
        "endpoint_describe",
        lambda: {"transport": "unix", "path": "/tmp/missing.sock", "host": None, "port": None, "token": None},
    )
    monkeypatch.setattr(_ipc, "sock_path", lambda: tmp_path / "missing.sock")
    assert cli_mod._cmd_status(SimpleNamespace(json=True), Config()) == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload["alive"] is False
    assert payload["endpoint"]["path"] == "/tmp/missing.sock"
    assert payload["probe_state"] == "not_running"


def test_cmd_status_json_marks_transient_probe_failure(tmp_path, capsys):
    """A daemon whose socket file survives but that never answers, with its
    ports free: `transient_probe_failed`, and the retry loop really ran."""
    from browserwright.daemon.probe import DaemonProbe

    sock = tmp_path / "daemon.sock"
    sock.write_text("", encoding="utf-8")
    calls = []

    class _Silent(DaemonProbe):
        retry_window = 0.05

        def ping(self, timeout):
            calls.append(timeout)
            return (None, None)

        def socket_present(self):
            return True

        # Hermetic: the real probe would reach the developer's own daemon on the
        # default ports (19989/19990) and report a port-held zombie instead.
        def listening_ports(self, ports):
            return []

        def endpoint(self):
            return {"transport": "unix", "path": str(sock),
                    "host": None, "port": None, "token": None}

        def facade(self):
            return (None, None)

        def sleep(self, seconds):
            pass

    assert cli_mod._cmd_status(SimpleNamespace(json=True), Config(),
                               probe=_Silent(Config())) == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload["probe_state"] == "transient_probe_failed"
    assert len(calls) > 1


def test_cmd_daemon_version_check_json_reports_consistent_versions(monkeypatch, capsys):
    monkeypatch.setattr(cli_mod, "_extension_relay_status", lambda cfg: {
        "daemon_version": "9.9.9",
        "extension_details": [
            {
                "install_id": "ext-1",
                "browserwright_version": "9.9.8",
                "daemon_version": "9.9.9",
                "version_drift": "patch",
            }
        ],
    })
    assert cli_mod.main(["version", "check", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
    assert payload["version"] == payload["extension_version"]
    assert payload["daemon_version"] == "9.9.9"
    assert payload["running_extensions"][0]["version_drift"] == "patch"


def test_binary_is_runnable_false_on_permission_error(monkeypatch, tmp_path):
    def fake_run(*args, **kwargs):
        raise PermissionError("nope")

    monkeypatch.setattr(platforms_mod.subprocess, "run", fake_run)
    assert platforms_mod._binary_is_runnable(tmp_path / "chrome") is False


def test_binary_is_runnable_checks_returncode(monkeypatch, tmp_path):
    monkeypatch.setattr(
        platforms_mod.subprocess,
        "run",
        lambda *a, **kw: SimpleNamespace(returncode=0),
    )
    assert platforms_mod._binary_is_runnable(tmp_path / "chrome") is True


def test_proc_start_time_parses_proc_stat(monkeypatch):
    class FakeProcStat:
        def __init__(self, path):
            self.path = path

        def exists(self):
            return True

        def read_text(self):
            fields = ["S", *[str(i) for i in range(1, 21)]]
            return "123 (name with ) paren) " + " ".join(fields)

    monkeypatch.setattr(platforms_mod, "Path", FakeProcStat)
    assert platforms_mod.proc_start_time(123) == "19"


def test_proc_start_time_falls_back_to_ps(monkeypatch):
    class MissingProcStat:
        def __init__(self, path):
            pass

        def exists(self):
            return False

    monkeypatch.setattr(platforms_mod, "Path", MissingProcStat)
    monkeypatch.setattr(
        platforms_mod.subprocess,
        "run",
        lambda *a, **kw: SimpleNamespace(returncode=0, stdout="Mon May 24 12:00:00 2026\n"),
    )
    assert platforms_mod.proc_start_time(456) == "Mon May 24 12:00:00 2026"


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("1", True),
        ("TRUE", True),
        ("yes", True),
        ("on", True),
        ("y", True),
        ("0", False),
        ("", False),
        ("no", False),
    ],
)
def test_truthy_env_variants(monkeypatch, value, expected):
    import browserwright.daemon.launch_chrome as lc_mod

    monkeypatch.setenv("BD_FLAG", value)
    assert lc_mod._truthy_env("BD_FLAG") is expected


def test_silent_context_swallows_only_oserror():
    import browserwright.daemon.launch_chrome as lc_mod

    with lc_mod._silent():
        raise OSError("cleanup")
    with pytest.raises(ValueError):
        with lc_mod._silent():
            raise ValueError("not cleanup")


@pytest.mark.asyncio
async def test_wait_for_chrome_ready_reads_devtools_active_port(tmp_path):
    import browserwright.daemon.launch_chrome as lc_mod

    (tmp_path / "DevToolsActivePort").write_text("51234\n/devtools/browser/abc\n")
    proc = SimpleNamespace(poll=lambda: None, returncode=None)
    assert await lc_mod._wait_for_chrome_ready(proc, tmp_path, requested_port=None, timeout=1) == (
        "51234",
        "/devtools/browser/abc",
    )


@pytest.mark.asyncio
async def test_wait_for_chrome_ready_terminates_live_process_on_timeout(tmp_path):
    import browserwright.daemon.launch_chrome as lc_mod

    terminated = {"value": False}

    class Proc:
        returncode = None

        def poll(self):
            return None

        def terminate(self):
            terminated["value"] = True

    with pytest.raises(Unavailable):
        await lc_mod._wait_for_chrome_ready(Proc(), tmp_path, requested_port=None, timeout=0)
    assert terminated["value"] is True


@pytest.mark.asyncio
async def test_resolver_records_unavailable_without_attempts(monkeypatch):
    import browserwright.daemon.resolver as resolver_mod

    class Backend:
        name = "env"

        async def resolve(self, timeout):
            raise Unavailable("plain failure")

    monkeypatch.setattr(resolver_mod, "all_backends", lambda cfg: [Backend()])
    with pytest.raises(Unavailable) as exc:
        await resolver_mod.resolve(load(env={}))
    assert exc.value.attempts == {"env": "plain failure"}


@pytest.mark.asyncio
async def test_resolver_auto_chain_skips_extension(monkeypatch):
    import browserwright.daemon.resolver as resolver_mod

    called: list[str] = []

    class Backend:
        def __init__(self, name, result=None):
            self.name = name
            self.result = result

        async def resolve(self, timeout):
            called.append(self.name)
            if self.result:
                return self.result
            raise Unavailable(self.name)

    chain = [
        Backend("extension"),
        Backend("env", ResolveResult("ws://env/", "env")),
    ]
    monkeypatch.setattr(resolver_mod, "all_backends", lambda cfg: chain)
    assert (await resolver_mod.resolve(load(env={}))).backend == "env"
    assert called == ["env"]


def test_userscript_payload_contains_chrome_registration_fields():
    from browserwright.daemon.userscripts import parse_userscript

    script = parse_userscript(
        "// ==UserScript==\n"
        "// @name Payload\n"
        "// @match https://example.test/*\n"
        "// @exclude https://example.test/private/*\n"
        "// @run-at document-start\n"
        "// ==/UserScript==\n"
        "console.log('x');\n"
    )
    payload = script.to_payload()
    assert payload["identity"] == "bd.userscripts/Payload"
    assert payload["excludeMatches"] == ["https://example.test/private/*"]
    assert payload["runAt"] == "document_start"
    assert payload["code"].startswith("console.log")


def test_userscript_invalid_exclude_warns_but_keeps_valid_match():
    from browserwright.daemon.userscripts import parse_userscript

    script = parse_userscript(
        "// ==UserScript==\n"
        "// @name ExcludeWarn\n"
        "// @match https://example.test/*\n"
        "// @exclude example.test/private/*\n"
        "// ==/UserScript==\n"
        "void 0;\n"
    )
    assert script.exclude_matches == []
    assert any("@exclude" in warning for warning in script.warnings)


def test_state_bind_unbind_session_updates_lookup_tables():
    from browserwright.daemon.server.state import DaemonState

    state = DaemonState("rdp")
    client = state.allocate_client("c")
    binding = state.bind_session(client.client_id, "local", "upstream", "target")
    state.claim_attacher("target", client.client_id, "local", "upstream")
    assert state.upstream_to_locals["upstream"] == [binding]
    assert client.sessions["local"] is binding
    assert state.unbind_session_by_local(client.client_id, "local") is binding
    assert client.sessions == {}
    assert "upstream" not in state.upstream_to_locals
    assert "target" not in state.attachers


@pytest.mark.asyncio
async def test_state_set_disconnected_clears_upstream_tied_tables():
    from browserwright.daemon.server.state import DaemonState

    state = DaemonState("rdp")
    client = state.allocate_client("c")
    state.bind_session(client.client_id, "local", "up", "target")
    state.claim_attacher("target", client.client_id, "local", "up")
    state.remember_request(1, client.client_id, 99, "Browser.getVersion")
    await state.set_disconnected()
    assert client.sessions == {}
    assert state.attachers == {}
    assert state.pending_requests == {}
    assert state.upstream_to_locals == {}


def test_state_note_target_info_ignores_non_string_target_id():
    from browserwright.daemon.server.state import DaemonState

    state = DaemonState("rdp")
    state.note_target_info({"targetId": 123, "type": "page", "url": "https://x/"})
    assert state.targets == {}


def test_daemon_context_for_rdp_lazily_creates_isolated_config(monkeypatch):
    from browserwright.daemon.server.daemon import Daemon, UpstreamContext
    from browserwright.daemon.server.state import DaemonState

    class Router:
        daemon = None

    shared = UpstreamContext(backend="extension", state=DaemonState("extension"), router=Router(), holder=object())
    made = []

    def make_context(backend, cfg, session_id):
        holder = type("Holder", (), {})()
        made.append((backend, cfg.backends.rdp.port, session_id))
        return UpstreamContext(backend=backend, state=DaemonState(backend), router=Router(), holder=holder, session_id=session_id)

    cfg = Config()
    cfg.backends.rdp.port = 9222
    monkeypatch.setattr(
        "browserwright.daemon.server.daemon.session_registry.get",
        lambda sid: {"backend": "rdp", "owner": "attach", "workspace": {"port": 9444}},
    )
    daemon = Daemon(cfg=cfg, shared_context=shared, make_context=make_context)
    ctx = daemon.context_for("s-rdp")
    assert ctx is daemon.context_for("s-rdp")
    assert made == [("rdp", 9444, "s-rdp")]
    assert cfg.backends.rdp.port == 9222
    assert ctx.router.daemon is daemon
    assert ctx.holder.rdp_owns_browser is False

    monkeypatch.setattr(
        "browserwright.daemon.server.daemon.session_registry.get",
        lambda sid: {"backend": "rdp", "owner": "create", "workspace": {"port": 9555}},
    )
    ctx2 = daemon.context_for("s-create")
    assert ctx2.holder.rdp_owns_browser is True


def test_daemon_context_for_unknown_or_non_rdp_uses_shared(monkeypatch):
    from browserwright.daemon.server.daemon import (
        Daemon,
        UnknownSessionError,
        UpstreamContext,
    )
    from browserwright.daemon.server.state import DaemonState

    class Router:
        daemon = None

    shared = UpstreamContext(backend="extension", state=DaemonState("extension"), router=Router(), holder=object())
    daemon = Daemon(cfg=Config(), shared_context=shared, make_context=lambda **kw: pytest.fail("should not create"))
    monkeypatch.setattr("browserwright.daemon.server.daemon.session_registry.get", lambda sid: None)
    assert daemon.context_for(None) is shared
    assert daemon.context_for("missing") is shared
    with pytest.raises(UnknownSessionError):
        daemon.context_for_required("missing")
    monkeypatch.setattr("browserwright.daemon.server.daemon.session_registry.get", lambda sid: {"backend": "extension"})
    assert daemon.context_for("extension-session") is shared
    assert daemon.context_for_required("extension-session") is shared


@pytest.mark.asyncio
async def test_daemon_teardown_missing_rdp_context_returns_false():
    from browserwright.daemon.server.daemon import Daemon, UpstreamContext
    from browserwright.daemon.server.state import DaemonState

    class Router:
        daemon = None

    shared = UpstreamContext(backend="extension", state=DaemonState("extension"), router=Router(), holder=object())
    daemon = Daemon(cfg=Config(), shared_context=shared, make_context=lambda **kw: pytest.fail("should not create"))
    assert await daemon.teardown_rdp_context("missing") is False


def test_proxy_json_helpers_and_cdp_result_unwrap():
    from browserwright.daemon.server import proxy

    assert proxy._json_safe('{"ok": true}') == {"ok": True}
    assert proxy._json_safe("[1, 2]") is None
    assert json.loads(proxy._error_response(1, -1, "bad"))["error"]["message"] == "bad"
    assert json.loads(proxy._result_response(2, {"ok": True}))["result"]["ok"] is True
    assert proxy._cmd_result({"id": 1, "result": {"value": 7}}) == {"value": 7}
    assert proxy._cmd_result({"id": 1, "result": None}) == {}
    with pytest.raises(RuntimeError):
        proxy._cmd_result({"id": 1, "error": {"message": "boom"}})
    with pytest.raises(RuntimeError):
        proxy._cmd_result("not a dict")


@pytest.mark.asyncio
async def test_router_release_client_detaches_sessions():
    from browserwright.daemon.server.proxy import Router
    from browserwright.daemon.server.state import DaemonState

    state = DaemonState("rdp")
    router = Router(state)
    sent = []
    router.update_upstream_send(lambda text: sent.append(json.loads(text)) or asyncio.sleep(0))
    client = state.allocate_client("c")
    state.bind_session(client.client_id, "primary", "up-primary", "target-1")
    released = await router.release_client(client.client_id)
    assert released is client
    assert [msg["params"]["sessionId"] for msg in sent] == ["up-primary"]
    assert client.client_id not in state.clients
