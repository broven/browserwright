from __future__ import annotations

import asyncio
import json
import subprocess
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from browserwright.daemon import cli as cli_mod
from browserwright.daemon import platforms as platforms_mod
from browserwright.daemon.backends.base import ResolveResult
from browserwright.daemon.config import Config, load
from browserwright.daemon.errors import (
    ChromeBinaryNotFound,
    DaemonError,
    Unavailable,
    UserError,
)


def test_cli_main_without_subcommand_prints_help(capsys):
    assert cli_mod.main([]) == 1
    captured = capsys.readouterr()
    assert "browserwright-daemon" in captured.err
    assert captured.out == ""


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

    monkeypatch.setitem(cli_mod._DISPATCH, "url", boom)
    assert cli_mod.main(["url", "--verbose"]) == 2
    captured = capsys.readouterr()
    assert "error: nothing resolved" in captured.err
    assert "env: unset" in captured.err
    assert "rdp: closed" in captured.err


def test_cmd_url_mode_b_proxy_unix_text(monkeypatch, capsys):
    from browserwright.daemon import _ipc

    monkeypatch.setattr(
        _ipc,
        "endpoint_describe",
        lambda: {"transport": "unix", "path": "/tmp/bd.sock", "host": None, "port": None, "token": None},
    )
    args = SimpleNamespace(mode_b_proxy=True, json=False)
    assert cli_mod._cmd_url(args, Config()) == 0
    assert capsys.readouterr().out == "/tmp/bd.sock\n"


def test_cmd_url_mode_b_proxy_tcp_without_port_exits_unavailable(monkeypatch, capsys):
    from browserwright.daemon import _ipc

    monkeypatch.setattr(
        _ipc,
        "endpoint_describe",
        lambda: {"transport": "tcp", "path": None, "host": "127.0.0.1", "port": None, "token": "tok"},
    )
    args = SimpleNamespace(mode_b_proxy=True, json=False)
    assert cli_mod._cmd_url(args, Config()) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "no daemon running" in captured.err


def test_cmd_status_json_includes_dead_endpoint(monkeypatch, capsys):
    from browserwright.daemon import _ipc

    monkeypatch.setattr(_ipc, "ping_status_sync", lambda timeout: (None, None))
    monkeypatch.setattr(
        _ipc,
        "endpoint_describe",
        lambda: {"transport": "unix", "path": "/tmp/missing.sock", "host": None, "port": None, "token": None},
    )
    assert cli_mod._cmd_status(SimpleNamespace(json=True), Config()) == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload["alive"] is False
    assert payload["endpoint"]["path"] == "/tmp/missing.sock"


def test_cmd_daemon_version_check_json_reports_consistent_versions(capsys):
    assert cli_mod.main(["version", "check", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
    assert payload["version"] == payload["extension_version"]


@pytest.mark.parametrize("json_mode", [False, True])
def test_cmd_active_tab_none_outputs_mode_specific_shape(monkeypatch, capsys, json_mode):
    async def fake_rpc(cfg, method, params, *, client_label, timeout, browser_session=None):
        assert method == "BrowserwrightDaemon.getActiveTab"
        assert params == {"bsSession": "bw-s"}
        assert browser_session == "bw-s"
        return {}

    monkeypatch.setattr(cli_mod, "_rpc_via_ws", fake_rpc)
    rc = cli_mod._cmd_active_tab(SimpleNamespace(json=json_mode, session="bw-s"), Config())
    captured = capsys.readouterr()
    assert rc == 2
    if json_mode:
        assert json.loads(captured.out)["accuracy"] == "unknown"
    else:
        assert captured.out == "\n"


def test_cmd_active_tab_text_outputs_tab_separated_info(monkeypatch, capsys):
    async def fake_rpc(cfg, method, params, *, client_label, timeout, browser_session=None):
        assert method == "BrowserwrightDaemon.getActiveTab"
        return {
            "targetId": "T1",
            "url": "https://example.test/",
            "title": "Example",
            "accuracy": "heuristic-recent-activate",
        }

    monkeypatch.setattr(cli_mod, "_rpc_via_ws", fake_rpc)
    assert cli_mod._cmd_active_tab(SimpleNamespace(json=False, session="bw-s"), Config()) == 0
    assert capsys.readouterr().out == "T1\thttps://example.test/\tExample\theuristic-recent-activate\n"


def test_cmd_close_tab_requires_identifier(capsys):
    args = SimpleNamespace(session="s1", session_id=None, target_id=None)
    assert cli_mod._cmd_close_tab(args, Config()) == 2
    assert "provide --session-id or --target-id" in capsys.readouterr().err


def test_cmd_open_background_success_prints_json(monkeypatch, capsys):
    monkeypatch.setattr(cli_mod, "_rpc_via_ws", lambda *a, **kw: object())
    def fake_run(awaitable):
        if hasattr(awaitable, "close"):
            awaitable.close()
        return {"targetId": "T", "tabId": 7}

    monkeypatch.setattr(cli_mod, "_run", fake_run)
    args = SimpleNamespace(url="https://example.test/", group="Agent", session="s1")
    assert cli_mod._cmd_open_background(args, Config()) == 0
    assert json.loads(capsys.readouterr().out) == {"tabId": 7, "targetId": "T"}


def test_cmd_end_session_passes_group_id(monkeypatch, capsys):
    seen = {}

    def fake_rpc(cfg, method, params, **kwargs):
        seen.update({"method": method, "params": params, "kwargs": kwargs})
        return object()

    monkeypatch.setattr(cli_mod, "_rpc_via_ws", fake_rpc)
    def fake_run(awaitable):
        if hasattr(awaitable, "close"):
            awaitable.close()
        return {"closed": [1], "kept": []}

    monkeypatch.setattr(cli_mod, "_run", fake_run)
    args = SimpleNamespace(session="s1", group_id=42)
    assert cli_mod._cmd_end_session(args, Config()) == 0
    assert seen["method"] == "BrowserwrightDaemon.endSession"
    assert seen["params"] == {"session": "s1", "groupId": 42}
    assert json.loads(capsys.readouterr().out)["closed"] == [1]


def test_pretty_doctor_prints_detail_warning_and_next(capsys):
    cli_mod._pretty_doctor(
        {
            "recommended": None,
            "backends": [
                {
                    "name": "env",
                    "available": False,
                    "ux_cost": "none",
                    "detail": "missing env",
                    "ux_warning": "no url",
                    "needs_user_action": "set BD_CDP_WS",
                }
            ],
        }
    )
    out = capsys.readouterr().out
    assert "recommended: (none available)" in out
    assert "detail: missing env" in out
    assert "warning: no url" in out
    assert "next: set BD_CDP_WS" in out


@pytest.mark.asyncio
async def test_fetch_targets_accepts_unwrapped_target_infos(monkeypatch):
    import browserwright.daemon.active_tab as at_mod

    class FakeClient:
        def __init__(self, ws_url):
            self.ws_url = ws_url

        async def start(self):
            return None

        async def send_raw(self, method):
            return {"targetInfos": [{"targetId": "T"}]}

        async def stop(self):
            return None

    monkeypatch.setattr(at_mod, "CDPClient", FakeClient)
    assert await at_mod._fetch_targets("ws://127.0.0.1:9222/devtools/browser/x", 1) == [{"targetId": "T"}]


@pytest.mark.asyncio
async def test_fetch_targets_non_list_payload_returns_empty(monkeypatch):
    import browserwright.daemon.active_tab as at_mod

    class FakeClient:
        def __init__(self, ws_url):
            pass

        async def start(self):
            return None

        async def send_raw(self, method):
            return {"result": {"targetInfos": "not-a-list"}}

        async def stop(self):
            return None

    monkeypatch.setattr(at_mod, "CDPClient", FakeClient)
    assert await at_mod._fetch_targets("ws://127.0.0.1:9222/devtools/browser/x", 1) == []


@pytest.mark.asyncio
async def test_fetch_targets_oserror_becomes_unavailable(monkeypatch):
    import browserwright.daemon.active_tab as at_mod

    class FakeClient:
        def __init__(self, ws_url):
            pass

        async def start(self):
            raise OSError("socket down")

    monkeypatch.setattr(at_mod, "CDPClient", FakeClient)
    with pytest.raises(Unavailable) as exc:
        await at_mod._fetch_targets("ws://127.0.0.1:9222/devtools/browser/x", 1)
    assert exc.value.attempts["active-tab"].startswith("OSError")


@pytest.mark.asyncio
async def test_silent_stop_swallows_client_stop_errors():
    import browserwright.daemon.active_tab as at_mod

    class BadStop:
        async def stop(self):
            raise RuntimeError("close raced")

    await at_mod._silent_stop(BadStop())


def test_localhost_bypass_proxy_adds_and_restores_no_proxy(monkeypatch):
    import browserwright.daemon.active_tab as at_mod

    monkeypatch.setenv("NO_PROXY", "example.com")
    with at_mod._localhost_bypass_proxy("ws://localhost:9222/devtools/browser/x"):
        assert "example.com" in at_mod.os.environ["NO_PROXY"]
        assert "127.0.0.1" in at_mod.os.environ["NO_PROXY"]
        assert "::1" in at_mod.os.environ["NO_PROXY"]
    assert at_mod.os.environ["NO_PROXY"] == "example.com"


def test_localhost_bypass_proxy_leaves_remote_hosts_alone(monkeypatch):
    import browserwright.daemon.active_tab as at_mod

    monkeypatch.delenv("NO_PROXY", raising=False)
    with at_mod._localhost_bypass_proxy("wss://remote.example/devtools/browser/x"):
        assert "NO_PROXY" not in at_mod.os.environ
    assert "NO_PROXY" not in at_mod.os.environ


def test_runtime_dir_prefers_xdg(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    assert platforms_mod.runtime_dir() == tmp_path


def test_runtime_dir_windows_uses_tempdir(monkeypatch, tmp_path):
    import tempfile

    monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
    monkeypatch.setattr(platforms_mod.platform, "system", lambda: "Windows")
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(tmp_path))
    assert platforms_mod.runtime_dir() == tmp_path


def test_cache_dir_prefers_xdg_cache_home(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    assert platforms_mod.cache_dir() == tmp_path / "browserwright-daemon"


@pytest.mark.parametrize(
    ("system", "expected"),
    [
        ("Darwin", "Google Chrome.app/Contents/MacOS/Google Chrome"),
        ("Windows", "Google/Chrome/Application/chrome.exe"),
        ("Linux", "/usr/bin/google-chrome"),
    ],
)
def test_chrome_binary_candidates_by_platform(monkeypatch, system, expected):
    monkeypatch.setattr(platforms_mod.platform, "system", lambda: system)
    paths = [str(p) for p in platforms_mod.chrome_binary_candidates()]
    assert any(expected in p for p in paths)


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


def test_discover_chrome_binary_darwin_prefers_app_candidate_before_path(monkeypatch, tmp_path):
    app = tmp_path / "Google Chrome"
    path_wrapper = tmp_path / "chromium"
    app.write_text("")
    path_wrapper.write_text("")
    calls: list[Path] = []

    monkeypatch.setattr(platforms_mod.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(platforms_mod, "chrome_binary_candidates", lambda: [app])
    monkeypatch.setattr(platforms_mod.shutil, "which", lambda name: str(path_wrapper))

    def runnable(path, timeout=3.0):
        calls.append(path)
        return True

    monkeypatch.setattr(platforms_mod, "_binary_is_runnable", runnable)
    assert platforms_mod.discover_chrome_binary() == app
    assert calls == [app]


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


def test_allocate_data_dir_persistent_uses_cache(monkeypatch, tmp_path):
    import browserwright.daemon.launch_chrome as lc_mod

    monkeypatch.setattr(lc_mod, "cache_dir", lambda: tmp_path / "cache")
    assert lc_mod._allocate_data_dir("prof", persistent=True) == tmp_path / "cache" / "profiles" / "prof"


def test_spawn_kwargs_posix_starts_new_session(monkeypatch):
    import browserwright.daemon.launch_chrome as lc_mod

    monkeypatch.setattr(lc_mod.platform, "system", lambda: "Linux")
    assert lc_mod._spawn_kwargs() == {"start_new_session": True}


def test_spawn_kwargs_windows_sets_creation_flags(monkeypatch):
    import browserwright.daemon.launch_chrome as lc_mod

    monkeypatch.setattr(lc_mod.platform, "system", lambda: "Windows")
    monkeypatch.setattr(lc_mod.subprocess, "CREATE_NEW_PROCESS_GROUP", 1, raising=False)
    monkeypatch.setattr(lc_mod.subprocess, "CREATE_NO_WINDOW", 2, raising=False)
    assert lc_mod._spawn_kwargs() == {"creationflags": 3}


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
async def test_resolver_auto_chain_skips_extension_and_cloud(monkeypatch):
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
        Backend("cloud"),
        Backend("env", ResolveResult("ws://env/", "env")),
    ]
    monkeypatch.setattr(resolver_mod, "all_backends", lambda cfg: chain)
    assert (await resolver_mod.resolve(load(env={}))).backend == "env"
    assert called == ["env"]


@pytest.mark.asyncio
async def test_cloud_provider_is_cached(monkeypatch):
    from browserwright.daemon.backends import cloud as cloud_mod
    from browserwright.daemon.backends.cloud import CloudBackend

    calls = []

    class Provider:
        pass

    monkeypatch.setattr(cloud_mod, "build_auth_provider", lambda kind, auth: calls.append((kind, auth)) or Provider())
    cfg = load(env={})
    cfg.backends.cloud.auth_kind = "bearer"
    cfg.backends.cloud.auth = {"token": "x"}
    backend = CloudBackend(cfg)
    assert backend._provider() is backend._provider()
    assert calls == [("bearer", {"token": "x"})]


@pytest.mark.asyncio
async def test_cloud_resolve_header_user_error_becomes_unavailable(monkeypatch):
    from browserwright.daemon.backends.cloud import CloudBackend

    class Provider:
        async def headers(self):
            raise UserError("token missing")

    cfg = load(env={})
    cfg.backends.cloud.endpoint = "https://cloud.example"
    backend = CloudBackend(cfg)
    monkeypatch.setattr(backend, "_provider", lambda: Provider())
    with pytest.raises(Unavailable) as exc:
        await backend.resolve(timeout=1)
    assert exc.value.attempts == {"cloud": "token missing"}


@pytest.mark.asyncio
async def test_cloud_resolve_http_client_error_becomes_unavailable(monkeypatch):
    from browserwright.daemon.backends import cloud as cloud_mod
    from browserwright.daemon.backends.cloud import CloudBackend

    class Client:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return None

        async def get(self, *a, **kw):
            raise OSError("network off")

    monkeypatch.setattr(cloud_mod.httpx, "AsyncClient", Client)
    cfg = load(env={})
    cfg.backends.cloud.endpoint = "https://cloud.example"
    with pytest.raises(Unavailable) as exc:
        await CloudBackend(cfg).resolve(timeout=1)
    assert "OSError" in exc.value.attempts["cloud"]


@pytest.mark.asyncio
async def test_cloud_resolve_invalid_json_counts_as_missing_ws(monkeypatch):
    from browserwright.daemon.backends import cloud as cloud_mod
    from browserwright.daemon.backends.cloud import CloudBackend

    class Response:
        status_code = 200

        def json(self):
            raise ValueError("not json")

    class Client:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return None

        async def get(self, *a, **kw):
            return Response()

    monkeypatch.setattr(cloud_mod.httpx, "AsyncClient", Client)
    cfg = load(env={})
    cfg.backends.cloud.endpoint = "https://cloud.example"
    with pytest.raises(Unavailable) as exc:
        await CloudBackend(cfg).resolve(timeout=1)
    assert "webSocketDebuggerUrl" in str(exc.value)


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
    binding = state.bind_session(client.client_id, "local", "upstream", "target", readonly=False)
    state.claim_attacher("target", client.client_id, "local", "upstream")
    assert state.upstream_to_locals["upstream"] == [binding]
    assert client.sessions["local"] is binding
    assert state.unbind_session_by_local(client.client_id, "local") is binding
    assert client.sessions == {}
    assert "upstream" not in state.upstream_to_locals
    assert "target" not in state.attachers


def test_state_reader_cleanup_leaves_primary_attacher():
    from browserwright.daemon.server.state import DaemonState

    state = DaemonState("rdp")
    owner = state.allocate_client("owner")
    reader = state.allocate_client("reader")
    state.bind_session(owner.client_id, "own", "up", "target", readonly=False)
    state.claim_attacher("target", owner.client_id, "own", "up")
    state.bind_session(reader.client_id, "read", "up", "target", readonly=True)
    state.add_reader("target", reader.client_id, "read")
    state.release_client(reader.client_id)
    assert state.attachers["target"].primary_client_id == owner.client_id
    assert state.attachers["target"].readers == []


@pytest.mark.asyncio
async def test_state_set_disconnected_clears_upstream_tied_tables():
    from browserwright.daemon.server.state import DaemonState

    state = DaemonState("rdp")
    client = state.allocate_client("c")
    state.bind_session(client.client_id, "local", "up", "target", readonly=False)
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


def test_state_note_activate_updates_activity_timestamp():
    from browserwright.daemon.server.state import DaemonState

    state = DaemonState("rdp")
    before = state.last_activity_at
    time.sleep(0.001)
    state.note_activate("T")
    assert state.last_activated_at["T"] >= before
    assert state.last_activity_at >= state.last_activated_at["T"]


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
    event = json.loads(proxy._event("Target.attachedToTarget", {"x": 1}, session_id="s"))
    assert event["sessionId"] == "s"
    assert proxy._cmd_result({"id": 1, "result": {"value": 7}}) == {"value": 7}
    assert proxy._cmd_result({"id": 1, "result": None}) == {}
    with pytest.raises(RuntimeError):
        proxy._cmd_result({"id": 1, "error": {"message": "boom"}})
    with pytest.raises(RuntimeError):
        proxy._cmd_result("not a dict")


@pytest.mark.asyncio
async def test_router_release_client_detaches_only_primary_sessions():
    from browserwright.daemon.server.proxy import Router
    from browserwright.daemon.server.state import DaemonState

    state = DaemonState("rdp")
    router = Router(state)
    sent = []
    router.update_upstream_send(lambda text: sent.append(json.loads(text)) or asyncio.sleep(0))
    client = state.allocate_client("c")
    state.bind_session(client.client_id, "primary", "up-primary", "target-1", readonly=False)
    state.bind_session(client.client_id, "reader", "up-reader", "target-2", readonly=True)
    released = await router.release_client(client.client_id)
    assert released is client
    assert [msg["params"]["sessionId"] for msg in sent] == ["up-primary"]
    assert client.client_id not in state.clients
