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


def test_daemon_context_for_preserves_multi_extension_shared_context(monkeypatch):
    from browserwright.daemon.server.daemon import Daemon, UpstreamContext
    from browserwright.daemon.server.state import DaemonState

    class Router:
        daemon = None

    shared = UpstreamContext(
        backend="extension", state=DaemonState("extension"),
        router=Router(), holder=object())
    daemon = Daemon(
        cfg=Config(), shared_context=shared,
        make_context=lambda **kw: pytest.fail("should not create"))
    monkeypatch.setattr(
        "browserwright.daemon.server.daemon.session_registry.get",
        lambda sid: {"id": sid, "backend": "extension"},
    )

    assert daemon.context_for_required("extension-a") is shared
    assert daemon.context_for_required("extension-b") is shared


def test_raw_cdp_sessions_get_own_contexts_whatever_the_shared_backend(
    monkeypatch,
):
    """#38's headline behavior: one daemon, N externally-attached browsers.

    An `env` record used to be servable only by a daemon whose *shared* backend
    was also `env`, and the ledger allowed one such session per daemon socket —
    which is exactly why driving N external profiles meant running N isolated
    daemons, each with its own XDG_RUNTIME_DIR, facade port and BD_CDP_WS.

    A raw-CDP session now carries its own endpoint and gets its own context,
    conditioned on nothing else. So an extension-backed daemon can hold several
    at once, each pointed somewhere different.
    """
    from types import SimpleNamespace

    from browserwright.daemon.server.daemon import (
        Daemon,
        UnknownSessionError,
        UpstreamContext,
    )
    from browserwright.daemon.server.state import DaemonState

    class Router:
        daemon = None

    shared = UpstreamContext(
        backend="extension", state=DaemonState("extension"),
        router=Router(), holder=object())

    def make_context(*, backend, cfg, session_id=None):
        return UpstreamContext(
            backend=backend, state=DaemonState(backend), router=Router(),
            holder=SimpleNamespace(_cfg=cfg), session_id=session_id)

    daemon = Daemon(cfg=Config(backend="extension"), shared_context=shared,
                    make_context=make_context)
    records = {
        "a": {"id": "a", "backend": "rdp", "owner": "attach",
              "workspace": {"url": "ws://cloud-a.example/cdp"}},
        "b": {"id": "b", "backend": "rdp", "owner": "attach",
              "workspace": {"url": "ws://cloud-b.example/cdp"}},
        "local": {"id": "local", "backend": "rdp", "owner": "create",
                  "workspace": {"port": 9444}},
        "retired": {"id": "retired", "backend": "env"},
    }
    monkeypatch.setattr(
        "browserwright.daemon.server.daemon.session_registry.get", records.get)

    ctx_a = daemon.context_for_required("a")
    ctx_b = daemon.context_for_required("b")
    ctx_local = daemon.context_for_required("local")

    # Each gets its own context, none of them the shared extension one.
    assert {id(ctx_a), id(ctx_b), id(ctx_local)}.isdisjoint({id(shared)})
    assert len({id(ctx_a), id(ctx_b), id(ctx_local)}) == 3
    # ...pointed at its own endpoint, with no cross-talk.
    assert ctx_a.holder._cfg.backends.rdp.endpoint == "ws://cloud-a.example/cdp"
    assert ctx_b.holder._cfg.backends.rdp.endpoint == "ws://cloud-b.example/cdp"
    assert ctx_local.holder._cfg.backends.rdp.endpoint is None
    assert ctx_local.holder._cfg.backends.rdp.port == 9444
    # The daemon-wide cfg is never mutated by any of that.
    assert daemon.cfg.backends.rdp.endpoint is None
    assert daemon.cfg.backends.rdp.port == 9222
    # Ownership still crosses the ledger→context boundary.
    assert ctx_a.holder.rdp_owns_browser is False
    assert ctx_local.holder.rdp_owns_browser is True
    # A retired backend value fails closed rather than inheriting anything.
    with pytest.raises(UnknownSessionError):
        daemon.context_for_required("retired")


@pytest.mark.parametrize("shared_backend,record_backend", [
    ("extension", "env"),
    ("env", "extension"),
    ("extension", "mystery"),
])
def test_daemon_context_for_rejects_backend_context_mismatch(
    monkeypatch, shared_backend, record_backend,
):
    from browserwright.daemon.server.daemon import (
        Daemon,
        UnknownSessionError,
        UpstreamContext,
    )
    from browserwright.daemon.server.state import DaemonState

    class Router:
        daemon = None

    shared = UpstreamContext(
        backend=shared_backend, state=DaemonState(shared_backend),
        router=Router(), holder=object())
    daemon = Daemon(
        cfg=Config(backend=shared_backend), shared_context=shared,
        make_context=lambda **kw: pytest.fail("should not create"))
    monkeypatch.setattr(
        "browserwright.daemon.server.daemon.session_registry.get",
        lambda sid: {"id": sid, "backend": record_backend},
    )

    with pytest.raises(UnknownSessionError):
        daemon.context_for_required("wrong-backend")


@pytest.mark.asyncio
async def test_daemon_termination_revokes_only_the_ended_session_and_gates_races(
    monkeypatch,
):
    from browserwright.daemon.server.daemon import (
        Daemon,
        UnknownSessionError,
        UpstreamContext,
    )
    from browserwright.daemon.server.state import DaemonState

    class Router:
        daemon = None

    class Registry:
        async def terminate_session(self, session_id, teardown, *, budget=None):
            assert session_id == "session-a"
            result = await teardown()
            return {"reaped": True}, result

    shared = UpstreamContext(
        backend="extension", state=DaemonState("extension"),
        router=Router(), holder=object())
    daemon = Daemon(
        cfg=Config(), shared_context=shared,
        make_context=lambda **kw: pytest.fail("should not create"))
    daemon.executors = Registry()
    monkeypatch.setattr(
        "browserwright.daemon.server.daemon.session_registry.get",
        lambda sid: {"id": sid, "backend": "extension"},
    )
    calls = []

    async def revoke(token):
        calls.append(("revoke", token))

    caller = object()
    other_control = object()
    facade = object()
    other_session = object()
    daemon.acquire_session_lease(
        "session-a", caller, lambda: revoke("caller"), kind="control")
    daemon.acquire_session_lease(
        "session-a", other_control, lambda: revoke("other-control"),
        kind="control")
    daemon.acquire_session_lease(
        "session-a", facade, lambda: revoke("facade"), kind="facade")
    daemon.acquire_session_lease(
        "session-b", other_session, lambda: revoke("session-b"),
        kind="facade")
    teardown_started = asyncio.Event()
    finish_teardown = asyncio.Event()

    async def teardown():
        calls.append(("teardown", "session-a"))
        teardown_started.set()
        await finish_teardown.wait()
        return {"ok": True, "backend": "extension"}

    ending = asyncio.create_task(daemon.terminate_session(
        "session-a", teardown, caller_token=caller))
    await teardown_started.wait()

    with pytest.raises(UnknownSessionError):
        daemon.acquire_session_lease(
            "session-a", object(), lambda: revoke("late-facade"),
            kind="facade")
    assert ("revoke", "session-b") not in calls
    assert ("revoke", "caller") not in calls
    assert calls[:3] == [
        ("revoke", "other-control"),
        ("revoke", "facade"),
        ("teardown", "session-a"),
    ]

    finish_teardown.set()
    reap, result = await ending
    assert reap["reaped"] is True
    assert result["ok"] is True
    assert daemon.session_is_terminal("session-a") is True

    # The endSession caller is closed only after its result has been sent.
    calls.append(("ack", "caller"))
    await daemon.revoke_session_lease(caller)
    assert calls[-2:] == [("ack", "caller"), ("revoke", "caller")]


@pytest.mark.asyncio
async def test_concurrent_session_terminators_do_not_deadlock_each_other(
    monkeypatch,
):
    from browserwright.daemon.server.daemon import Daemon, UpstreamContext
    from browserwright.daemon.server.state import DaemonState

    class Router:
        daemon = None

    class Registry:
        async def terminate_session(self, session_id, teardown, *, budget=None):
            return {"reaped": True}, await teardown()

    shared = UpstreamContext(
        backend="extension", state=DaemonState("extension"),
        router=Router(), holder=object())
    daemon = Daemon(
        cfg=Config(), shared_context=shared,
        make_context=lambda **kw: pytest.fail("should not create"))
    daemon.executors = Registry()
    monkeypatch.setattr(
        "browserwright.daemon.server.daemon.session_registry.get",
        lambda sid: {"id": sid, "backend": "extension"},
    )

    first_token = object()
    second_token = object()
    first_task = None
    second_task = None

    async def revoke_first():
        assert first_task is not None
        await first_task

    async def revoke_second():
        assert second_task is not None
        await second_task

    daemon.acquire_session_lease(
        "session-a", first_token, revoke_first, kind="control")
    daemon.acquire_session_lease(
        "session-a", second_token, revoke_second, kind="control")

    async def teardown():
        return {"ok": True, "backend": "extension"}

    first_task = asyncio.create_task(daemon.terminate_session(
        "session-a", teardown, caller_token=first_token))
    second_task = asyncio.create_task(daemon.terminate_session(
        "session-a", teardown, caller_token=second_token))

    first, second = await asyncio.wait_for(
        asyncio.gather(first_task, second_task), timeout=0.5)
    assert first[1]["ok"] is True
    assert second[1]["ok"] is True


@pytest.mark.asyncio
async def test_daemon_teardown_missing_rdp_context_returns_false():
    from browserwright.daemon.server.daemon import Daemon, UpstreamContext
    from browserwright.daemon.server.state import DaemonState

    class Router:
        daemon = None

    shared = UpstreamContext(backend="extension", state=DaemonState("extension"), router=Router(), holder=object())
    daemon = Daemon(cfg=Config(), shared_context=shared, make_context=lambda **kw: pytest.fail("should not create"))
    assert await daemon.teardown_rdp_context("missing") is False


@pytest.mark.asyncio
async def test_daemon_teardown_failure_retains_rdp_context_for_retry():
    from browserwright.daemon.server.daemon import Daemon, UpstreamContext
    from browserwright.daemon.server.state import DaemonState

    class Router:
        daemon = None

    class Holder:
        def __init__(self):
            self.killed = False

        def _kill_rdp_chrome(self):
            self.killed = True
            return True

        async def trigger_close(self, _reason):
            assert self.killed is True
            raise RuntimeError("Chrome refused to terminate")

        async def abort_rdp_teardown(self):
            return None

    shared = UpstreamContext(
        backend="extension", state=DaemonState("extension"),
        router=Router(), holder=object())
    daemon = Daemon(
        cfg=Config(), shared_context=shared,
        make_context=lambda **kw: pytest.fail("should not create"))
    ctx = UpstreamContext(
        backend="rdp", state=DaemonState("rdp"),
        router=Router(), holder=Holder())
    daemon.contexts["sess"] = ctx

    with pytest.raises(RuntimeError, match="refused to terminate"):
        await daemon.teardown_rdp_context("sess")
    assert daemon.contexts["sess"] is ctx


@pytest.mark.asyncio
async def test_daemon_teardown_budget_restores_retryable_rdp_context():
    import time

    from browserwright.daemon.server.daemon import Daemon, UpstreamContext
    from browserwright.daemon.server.state import DaemonState, UpstreamPhase

    class Router:
        daemon = None

    state = DaemonState("rdp")

    class Holder:
        def __init__(self):
            self.killed = False

        def _kill_rdp_chrome(self):
            self.killed = True
            return True

        async def trigger_close(self, _reason):
            await state.begin_closing("skill_disconnect")
            await asyncio.Event().wait()

        async def abort_rdp_teardown(self):
            await state.set_disconnected()

    shared = UpstreamContext(
        backend="extension", state=DaemonState("extension"),
        router=Router(), holder=object())
    daemon = Daemon(
        cfg=Config(), shared_context=shared,
        make_context=lambda **kw: pytest.fail("should not create"))
    holder = Holder()
    ctx = UpstreamContext(
        backend="rdp", state=state, router=Router(), holder=holder)
    daemon.contexts["sess"] = ctx

    ended = await daemon.teardown_rdp_context(
        "sess", deadline=time.monotonic() + 0.01)

    assert ended is False
    assert holder.killed is True
    assert daemon.contexts["sess"] is ctx
    assert state.upstream_phase is UpstreamPhase.DISCONNECTED


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


# ---- issue #32: initiate-then-join contract --------------------------------


class _InitiateRegistry:
    """Fake registry speaking the issue #32 initiate contract: terminate
    returns at the initiate boundary, the final result comes from
    await_termination (mirrors the real ExecutorRegistry)."""

    def __init__(self, teardown):
        self.teardown = teardown
        self.task: asyncio.Task | None = None
        self.result: dict = {}

    async def terminate_session(self, session_id, teardown, *, budget=None):
        if self.result:
            return ({"killed": False, "reaped": True, "matched": True,
                     "executor_id": None}, dict(self.result))
        if self.task is not None:
            result = await self.task
            return ({"killed": False, "reaped": True, "matched": True,
                     "executor_id": None}, dict(result))
        self.task = asyncio.create_task(self._run(teardown))
        return ({"killed": False, "reaped": True, "matched": True,
                 "executor_id": None},
                {"ok": True, "initiated": True, "phase": "terminating"})

    async def _run(self, teardown):
        result = await teardown()
        self.result = dict(result)
        return result

    async def await_termination(self, session_id):
        if self.task is not None:
            await self.task
        return dict(self.result)


def _make_daemon(registry):
    from browserwright.daemon.server.daemon import Daemon, UpstreamContext
    from browserwright.daemon.server.state import DaemonState

    class Router:
        daemon = None

    shared = UpstreamContext(
        backend="extension", state=DaemonState("extension"),
        router=Router(), holder=object())
    daemon = Daemon(
        cfg=Config(), shared_context=shared,
        make_context=lambda **kw: pytest.fail("should not create"))
    daemon.executors = registry
    return daemon


@pytest.mark.asyncio
async def test_daemon_initiate_returns_at_boundary_and_watcher_publishes_end(
    monkeypatch,
):
    """wait=False (the verb handler's contract): terminate_session returns at
    the initiate boundary with phase=terminating, and the daemon's watcher
    publishes ended + the final result once the background teardown completes
    — so `ps` is truthful even if no caller ever polls."""
    from browserwright.daemon.server.daemon import UnknownSessionError

    started = asyncio.Event()
    allow = asyncio.Event()

    async def teardown():
        started.set()
        await allow.wait()
        return {"ok": True, "closed": [1], "backend": "extension"}

    registry = _InitiateRegistry(teardown)
    daemon = _make_daemon(registry)
    monkeypatch.setattr(
        "browserwright.daemon.server.daemon.session_registry.get",
        lambda sid: {"id": sid, "backend": "extension"},
    )

    reap, result = await daemon.terminate_session(
        "session-a", teardown, wait=False)
    assert reap["reaped"] is True
    assert result == {"ok": True, "initiated": True, "phase": "terminating"}
    assert daemon.session_is_terminal("session-a") is False
    # Leases are gated from initiate time: only control connections allowed.
    with pytest.raises(UnknownSessionError):
        daemon.acquire_session_lease(
            "session-a", object(), lambda: asyncio.sleep(0), kind="facade")

    allow.set()
    await asyncio.wait_for(registry.task, timeout=1.0)
    await asyncio.sleep(0)  # let the watcher publish
    assert daemon.session_is_terminal("session-a") is True
    assert daemon._session_results["session-a"]["ok"] is True
    assert daemon._session_results["session-a"]["closed"] == [1]


@pytest.mark.asyncio
async def test_daemon_retry_joins_inflight_termination_and_returns_final(
    monkeypatch,
):
    """A retried terminate_session against a terminating session joins the
    in-flight teardown and returns the FINAL result (the CLI's poll), while a
    fresh terminate afterwards gets the cached tombstone result."""
    started = asyncio.Event()
    allow = asyncio.Event()

    async def teardown():
        started.set()
        await allow.wait()
        return {"ok": True, "closed": [1, 2], "backend": "extension"}

    registry = _InitiateRegistry(teardown)
    daemon = _make_daemon(registry)
    monkeypatch.setattr(
        "browserwright.daemon.server.daemon.session_registry.get",
        lambda sid: {"id": sid, "backend": "extension"},
    )

    first = asyncio.create_task(daemon.terminate_session(
        "session-a", teardown, wait=False))
    await asyncio.wait_for(started.wait(), timeout=1.0)
    retry = asyncio.create_task(daemon.terminate_session(
        "session-a", teardown, wait=False))

    allow.set()
    _reap, initiated = await first
    assert initiated["initiated"] is True
    _reap2, joined = await asyncio.wait_for(retry, timeout=1.0)
    assert joined["ok"] is True
    assert joined["closed"] == [1, 2]
    assert joined.get("initiated") is not True
    assert daemon.session_is_terminal("session-a") is True

    # A subsequent terminate is idempotent: the cached tombstone result.
    _reap3, cached = await daemon.terminate_session(
        "session-a", teardown, wait=False)
    assert cached["ok"] is True
    assert cached["closed"] == [1, 2]
