"""Curated offline coverage tests for CLI/runtime helper branches."""
from __future__ import annotations

import json
import subprocess
from types import SimpleNamespace

import pytest


def test_parse_kv_args_coerces_json_space_values_and_flags():
    from browserwright import cli

    parsed = cli._parse_kv_args([
        "ignored",
        "--count",
        "3",
        "--enabled=false",
        "--items=[1,2]",
        "--flag",
    ])

    assert parsed == {
        "count": 3,
        "enabled": False,
        "items": [1, 2],
        "flag": True,
    }


def test_parse_execute_args_supports_short_and_long_forms():
    from browserwright import cli

    assert cli._parse_execute_args(["-s", "1", "-e", "print(1)"]) == (
        "1",
        "print(1)",
        None,
    )
    assert cli._parse_execute_args(["--session=2", "--execute=print(2)"]) == (
        "2",
        "print(2)",
        None,
    )
    assert cli._parse_execute_args(["-s", "1"])[2] == (
        "missing code: pass -e '<python>', -f <path>, or --code-stdin"
    )


def test_parse_execute_args_reads_code_file(tmp_path):
    from browserwright import cli

    script = tmp_path / "script.py"
    script.write_text("print('from file')\n")

    assert cli._parse_execute_args(["-s", "1", "-f", str(script)]) == (
        "1",
        "print('from file')\n",
        None,
    )


def test_parse_execute_args_rejects_multiple_code_sources():
    from browserwright import cli

    assert cli._parse_execute_args(["-s", "1", "-e", "print(1)", "--code-stdin"])[2] == (
        "pass only one of -e, -f, or --code-stdin"
    )


def test_parse_execute_args_reads_code_stdin(monkeypatch):
    from io import StringIO
    from browserwright import cli

    monkeypatch.setattr(cli.sys, "stdin", StringIO("print('stdin')\n"))

    assert cli._parse_execute_args(["--session=1", "--code-stdin"]) == (
        "1",
        "print('stdin')\n",
        None,
    )


def test_cmd_execute_dispatches_to_inline_run_code(monkeypatch):
    from browserwright import cli
    from browserwright.repl import inline

    calls = []
    monkeypatch.setattr(
        inline,
        "run_code",
        lambda code, *, session_id: calls.append((session_id, code)) or 0,
    )

    assert cli._cmd_execute(["-s", "abc", "-e", "print('ok')"]) == 0
    assert calls == [("abc", "print('ok')")]


def test_global_session_prefix_dispatches_task(monkeypatch, tmp_bs_home, capsys):
    from browserwright import cli
    from browserwright import session_registry as reg
    from browserwright import task_runner

    sid = reg.allocate(backend="rdp", owner="create", name="job")
    monkeypatch.setattr(task_runner, "run_task", lambda site, name, **kw: {
        "site": site,
        "name": name,
        "kwargs": kw,
    })

    with pytest.raises(SystemExit) as exc:
        cli.main(["-s", sid, "task", "example.com/check", "--count=2", "--output", "json"])
    assert exc.value.code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload == {
        "site": "example.com",
        "name": "check",
        "kwargs": {"count": 2},
    }


def test_cmd_version_check_json_reports_consistent_versions(monkeypatch, capsys):
    from browserwright import cli

    monkeypatch.setattr(cli, "_extension_relay_status", lambda: {
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
    assert cli._cmd_version(["check", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
    assert payload["version"] == payload["extension_version"]
    assert payload["extension_protocol_version"] == "1"
    assert payload["daemon_version"] == "9.9.9"
    assert payload["running_extensions"][0]["version_drift"] == "patch"


def test_cmd_release_status_json(monkeypatch, capsys):
    from browserwright import cli, release_install

    monkeypatch.setattr(
        release_install,
        "status",
        lambda: {
            "schema_version": 1,
            "installed_version": "0.6.0",
            "daemon": {"alive": True, "version": "0.5.9", "restart_required": True},
            "skill": [],
            "releases": [],
        },
    )

    assert cli._cmd_release(["status", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["daemon"]["restart_required"] is True


@pytest.mark.parametrize(
    "args, err",
    [
        ([], "usage: browserwright -s <session-id> task"),
        (["missing-slash"], "task spec must be"),
    ],
)
def test_cmd_task_rejects_bad_invocation(args, err, capsys, tmp_bs_home):
    from browserwright import cli

    assert cli._cmd_task(args) == 1
    assert err in capsys.readouterr().err


def test_cmd_task_reports_missing_task(monkeypatch, capsys, tmp_bs_home):
    from browserwright import cli, task_runner
    from browserwright import session_registry as reg

    def missing(site, name, **kwargs):
        raise FileNotFoundError(f"{site}/{name}")

    monkeypatch.setattr(task_runner, "run_task", missing)
    sid = reg.allocate(backend="rdp", owner="create")

    assert cli._cmd_task(["--session", sid, "site/name"]) == 1
    assert "task not found" in capsys.readouterr().err


def test_cmd_task_reports_crash(monkeypatch, capsys, tmp_bs_home):
    from browserwright import cli, task_runner
    from browserwright import session_registry as reg

    def crashed(site, name, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(task_runner, "run_task", crashed)
    sid = reg.allocate(backend="rdp", owner="create")

    assert cli._cmd_task(["--session", sid, "site/name"]) == 3
    assert "task crashed" in capsys.readouterr().err


def test_cmd_task_binds_session_and_outputs_json(monkeypatch, tmp_bs_home, capsys):
    from browserwright import cli, task_runner
    from browserwright import session_registry as reg

    sid = reg.allocate(backend="rdp", owner="create")
    monkeypatch.setattr(task_runner, "run_task", lambda site, name, **kwargs: {
        "site": site,
        "name": name,
        "limit": kwargs["limit"],
    })

    assert cli._cmd_task(["--session", sid, "site/name", "--limit=3", "--output", "json"]) == 0
    assert json.loads(capsys.readouterr().out) == {
        "site": "site",
        "name": "name",
        "limit": 3,
    }


def test_cmd_doctor_human_failure_prints_fixes(monkeypatch, capsys):
    from browserwright import cli, health

    monkeypatch.setattr(
        health,
        "doctor_checks",
        lambda: {
            "checks": [
                {"name": "daemon", "status": "fail", "message": "off", "fix": "start it"},
                {"name": "helpers", "status": "pass", "message": "ok", "fix": ""},
            ]
        },
    )

    assert cli._cmd_doctor([]) == 1
    out = capsys.readouterr().out
    assert "fix: start it" in out
    assert "doctor: FAIL" in out


def test_cmd_index_rejects_unknown_subcommand(capsys):
    from browserwright import cli

    assert cli._cmd_index(["refresh"]) == 1
    assert "usage: browserwright index rebuild" in capsys.readouterr().err


@pytest.mark.parametrize(
    "args, expected",
    [
        ([], "usage: browserwright memory"),
        (["show"], "specify --site=SITE or --global"),
        (["forget", "--global"], "usage: memory forget"),
        (["replace", "--pattern=x", "--global"], "usage: memory replace"),
        (["unknown"], "unknown memory subcommand"),
    ],
)
def test_cmd_memory_usage_errors(args, expected, capsys, tmp_bs_home):
    from browserwright import cli

    assert cli._cmd_memory(args) == 1
    assert expected in capsys.readouterr().err


def test_cmd_memory_replace_dry_run_then_confirm(tmp_bs_home, capsys):
    from browserwright import cli
    from browserwright.memory import global_memory

    global_memory().append("old browser note")

    assert cli._cmd_memory([
        "replace",
        "--pattern=old browser",
        "--with=new browser note",
        "--global",
    ]) == 0
    out = capsys.readouterr().out
    assert "would remove 1 line" in out
    assert "run again with --yes" in out
    assert "old browser note" in global_memory().read()["body"]

    assert cli._cmd_memory([
        "replace",
        "--pattern=old browser",
        "--with=new browser note",
        "--global",
        "--yes",
    ]) == 0
    body = global_memory().read()["body"]
    assert "old browser note" not in body
    assert "new browser note" in body


def test_cmd_session_usage_errors(capsys, tmp_bs_home):
    from browserwright import cli

    assert cli._cmd_session([]) == 1
    assert "usage: browserwright session" in capsys.readouterr().err

    assert cli._cmd_session(["new", "--backend=bogus", "--name=x"]) == 1
    assert "--backend=<extension|rdp|env>" in capsys.readouterr().err

    assert cli._cmd_session(["bogus"]) == 1
    assert "unknown session subcommand" in capsys.readouterr().err


def test_cmd_session_reset_recycles_executor(tmp_bs_home, monkeypatch, capsys):
    from browserwright import cli, session_create, session_registry as reg

    sid = reg.allocate(backend="rdp", owner="attach", name="attached")
    calls = []
    monkeypatch.setattr(
        session_create,
        "reset_executor",
        lambda rec: calls.append(rec["id"]) or f"reset {rec['id']}",
    )

    assert cli._cmd_session(["reset", sid]) == 0
    assert calls == [sid]
    assert capsys.readouterr().out == f"reset {sid}\n"


def test_global_session_prefix_dispatches_whoami(tmp_bs_home, capsys):
    from browserwright import cli, session_registry as reg

    sid = reg.allocate(backend="rdp", owner="attach", name="attached")

    with pytest.raises(SystemExit) as exc:
        cli.main(["-s", sid, "whoami"])
    assert exc.value.code == 0
    assert json.loads(capsys.readouterr().out)["id"] == sid


def test_global_session_prefix_dispatches_session_end(tmp_bs_home, monkeypatch, capsys):
    from browserwright import cli, session_create, session_registry as reg

    sid = reg.allocate(backend="rdp", owner="attach", name="attached")
    calls = []
    monkeypatch.setattr(
        session_create,
        "end",
        lambda rec: calls.append(rec["id"]) or f"ended {rec['id']}",
    )

    with pytest.raises(SystemExit) as exc:
        cli.main(["-s", sid, "session", "end"])
    assert exc.value.code == 0
    assert calls == [sid]
    assert capsys.readouterr().out == f"ended {sid}\n"


def test_session_inner_session_overrides_global_prefix(tmp_bs_home, monkeypatch, capsys):
    from browserwright import cli, session_create, session_registry as reg

    outer = reg.allocate(backend="rdp", owner="attach", name="outer")
    inner = reg.allocate(backend="rdp", owner="attach", name="inner")
    calls = []
    monkeypatch.setattr(
        session_create,
        "end",
        lambda rec: calls.append(rec["id"]) or f"ended {rec['id']}",
    )

    with pytest.raises(SystemExit) as exc:
        cli.main(["-s", outer, "session", "--session", inner, "end"])
    assert exc.value.code == 0
    assert calls == [inner]
    assert capsys.readouterr().out == f"ended {inner}\n"


def test_session_reset_uses_bd_session_fallback(tmp_bs_home, monkeypatch, capsys):
    from browserwright import cli, session_create, session_registry as reg

    sid = reg.allocate(backend="rdp", owner="attach", name="attached")
    monkeypatch.setenv("BD_SESSION", sid)
    calls = []
    monkeypatch.setattr(
        session_create,
        "reset_executor",
        lambda rec: calls.append(rec["id"]) or f"reset {rec['id']}",
    )

    assert cli._cmd_session(["reset"]) == 0
    assert calls == [sid]
    assert capsys.readouterr().out == f"reset {sid}\n"


def test_cmd_session_new_stderr_keeps_stdout_bare(monkeypatch, tmp_bs_home, capsys):
    from browserwright import cli, session_create

    monkeypatch.setattr(session_create, "new", lambda **kwargs: "17")

    assert cli._cmd_session(["new", "--backend=rdp", "--name=job"]) == 0
    streams = capsys.readouterr()
    assert streams.out == "17\n"
    assert streams.err == "OK: session 17 created\n"


def test_cmd_session_new_accepts_env_backend(monkeypatch, tmp_bs_home, capsys):
    """issue #20: `session new --backend=env` is accepted and routed to
    session_create.new (previously rejected — the gate only allowed
    extension|rdp)."""
    from browserwright import cli, session_create

    seen = {}
    monkeypatch.setattr(session_create, "new",
                        lambda **kw: seen.update(kw) or "7")

    assert cli._cmd_session(["new", "--backend=env", "--name=cloak"]) == 0
    assert seen["backend"] == "env"
    assert capsys.readouterr().out == "7\n"


def test_cmd_userscript_verify_skips_reload_after_push_failure(monkeypatch, capsys):
    from browserwright import cli

    calls = []

    def fake_run(argv):
        calls.append(argv)
        return SimpleNamespace(returncode=7)

    monkeypatch.setattr(cli.subprocess, "run", fake_run)

    assert cli._cmd_userscript(["push", "script.js", "--verify"]) == 7
    assert calls == [["browserwright-daemon", "userscript", "push", "script.js"]]
    assert capsys.readouterr().out == ""


def test_cmd_userscript_verify_binds_bd_session(monkeypatch, tmp_bs_home, capsys):
    from browserwright import cli
    from browserwright import session_registry as reg

    sid = reg.allocate(backend="rdp", owner="create")
    monkeypatch.setenv("BD_SESSION", sid)
    calls = []

    def fake_run(argv, **kwargs):
        calls.append(argv)
        if argv[:3] == ["browserwright-daemon", "status", "--json"]:
            return SimpleNamespace(returncode=1, stdout="", stderr="")
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(cli.subprocess, "run", fake_run)
    monkeypatch.setattr("browserwright.mode_b_client.ModeBClient.is_alive", lambda self: False)

    # --verify drives the Playwright path: reload the bound page, screenshot
    # it, print the path, and ALWAYS close the handle.
    events = []

    class FakePage:
        def reload(self, *, wait_until=None):
            events.append(("reload", wait_until))

        def screenshot(self, *, path):
            events.append(("screenshot", path))

    class FakeHandle:
        page = FakePage()

        def close(self):
            events.append(("close", None))

    monkeypatch.setattr(
        "browserwright.repl.playwright_handle.PlaywrightHandle", FakeHandle)
    monkeypatch.setattr(cli, "_fresh_screenshot_path", lambda: "/tmp/shot.png")

    assert cli._cmd_userscript(["push", "script.js", "--verify"]) == 0
    assert calls == [["browserwright-daemon", "userscript", "push", "script.js"]]
    assert capsys.readouterr().out == "/tmp/shot.png\n"
    assert events == [
        ("reload", "load"), ("screenshot", "/tmp/shot.png"), ("close", None),
    ]


def test_daemon_doctor_synthetic_for_spawn_failure(monkeypatch):
    from browserwright import health

    def fail(*args, **kwargs):
        raise FileNotFoundError("missing daemon")

    monkeypatch.setattr(health.subprocess, "run", fail)

    info = health.daemon_doctor()
    assert info["skill_synthetic"] is True
    assert info["backends"] == []
    assert "missing daemon" in info["error"]


def test_daemon_doctor_synthetic_for_nonzero_exit(monkeypatch):
    from browserwright import health

    monkeypatch.setattr(
        health.subprocess,
        "run",
        lambda *a, **k: SimpleNamespace(returncode=4, stdout="", stderr="bad daemon\n"),
    )

    info = health.daemon_doctor()
    assert info["exit_code"] == 4
    assert info["error"] == "bad daemon"


def test_daemon_doctor_synthetic_for_invalid_json(monkeypatch):
    from browserwright import health

    monkeypatch.setattr(
        health.subprocess,
        "run",
        lambda *a, **k: SimpleNamespace(returncode=0, stdout="{nope", stderr=""),
    )

    assert health.daemon_doctor()["error"] == "doctor output was not JSON"


def test_doctor_checks_warns_on_unknown_schema_and_fails_without_backend(monkeypatch):
    from browserwright import health

    monkeypatch.setattr(
        health,
        "daemon_doctor",
        lambda: {"schema_version": 99, "backends": []},
    )

    checks = health.doctor_checks()["checks"]
    by_name = {check["name"]: check for check in checks}
    assert by_name["daemon"]["status"] == "pass"
    assert by_name["daemon_schema"]["status"] == "warn"
    assert by_name["backend"]["status"] == "fail"
    assert by_name["backend"]["fix"]


def test_doctor_checks_extension_unavailable_is_actionable_warn(monkeypatch):
    from browserwright import health

    monkeypatch.setattr(
        health,
        "daemon_doctor",
        lambda: {
            "schema_version": 2,
            "backends": [
                {
                    "name": "extension",
                    "available": False,
                    "ux_warning": "not connected",
                    "needs_user_action": "load extension",
                }
            ],
        },
    )

    extension = next(c for c in health.doctor_checks()["checks"] if c["name"] == "extension")
    assert extension["status"] == "warn"
    assert extension["message"] == "not connected"
    assert extension["fix"] == "load extension"


def test_doctor_checks_surfaces_available_backend_ux_warning(monkeypatch):
    from browserwright import health

    monkeypatch.setattr(
        health,
        "daemon_doctor",
        lambda: {
            "schema_version": 2,
            "backends": [
                {
                    "name": "extension",
                    "available": True,
                    "ux_warning": "extension version mismatch",
                    "needs_user_action": "reload extension",
                    "ws_url": "ws://relay",
                }
            ],
        },
    )

    checks = health.doctor_checks()["checks"]
    warning = next(c for c in checks if c["name"] == "extension_warning")
    assert warning["status"] == "warn"
    assert warning["message"] == "extension version mismatch"
    assert warning["fix"] == "reload extension"


def test_session_create_run_returns_one_for_spawn_errors(monkeypatch):
    from browserwright import session_create

    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired(args[0], timeout=10)

    monkeypatch.setattr(session_create.subprocess, "run", timeout)

    assert session_create._run(["browserwright-daemon", "end-session"]) == 1


def test_session_create_reap_tears_down_before_removing_ledger(tmp_bs_home, monkeypatch):
    from browserwright import session_create, session_registry as reg

    create_sid = reg.allocate(backend="rdp", owner="create", name="owned")
    attach_sid = reg.allocate(backend="rdp", owner="attach", name="attached")
    ext_sid = reg.allocate(backend="extension", owner="attach", name="ext")
    reg._with_entry(create_sid, lambda e: e.update(last_seen=0.0))
    reg._with_entry(attach_sid, lambda e: e.update(last_seen=0.0))
    reg._with_entry(ext_sid, lambda e: e.update(last_seen=0.0))
    closed = []
    reaped = []
    ext_closed = []

    def _close_browser(rec):
        assert reg.get(rec["id"]) is not None
        closed.append(rec["id"])

    def _reap_executor(rec):
        assert reg.get(rec["id"]) is not None
        reaped.append(rec["id"])

    def _end_extension(rec):
        assert reg.get(rec["id"]) is not None
        ext_closed.append(rec["id"])

    monkeypatch.setattr(session_create, "_close_browser", _close_browser)
    monkeypatch.setattr(session_create, "_reap_executor", _reap_executor)
    monkeypatch.setattr(session_create, "_end_extension_workspace", _end_extension)

    pruned = session_create.reap(idle_seconds=1)

    assert {rec["id"] for rec in pruned} == {create_sid, attach_sid, ext_sid}
    assert reaped == [create_sid, attach_sid, ext_sid]
    assert closed == [create_sid]
    assert ext_closed == [ext_sid]
    assert reg.get(create_sid) is None
    assert reg.get(attach_sid) is None
    assert reg.get(ext_sid) is None


def test_session_create_reset_executor_keeps_ledger(tmp_bs_home, monkeypatch):
    from browserwright import session_create, session_registry as reg

    sid = reg.allocate(backend="rdp", owner="attach", name="attached")
    calls = []
    monkeypatch.setattr(session_create, "_ensure_daemon_running", lambda: calls.append(["ensure"]))
    monkeypatch.setattr(session_create, "_run", lambda cmd: calls.append(cmd) or 0)

    message = session_create.reset_executor(reg.get(sid))

    assert calls == [["ensure"], ["browserwright-daemon", "kill-executor", "--session", sid]]
    assert "left untouched" in message
    assert reg.get(sid) is not None


def test_session_create_end_extension_threads_group_id(tmp_bs_home, monkeypatch):
    from browserwright import session_create, session_registry as reg

    sid = reg.allocate(backend="extension", owner="attach", name="shared")
    reg.update(sid, runtime={"group_id": 12})
    calls = []
    monkeypatch.setattr(session_create, "_run", lambda cmd: calls.append(cmd) or 0)

    message = session_create.end(reg.get(sid))

    # Extension is attach-owned: end() closes the session's owned tabs
    # (end-session, threading the durable group-id) AND best-effort reaps the
    # session's resident Phase B executor (kill-executor) so it doesn't leak —
    # the browser itself stays running.
    assert calls == [
        [
            "browserwright-daemon",
            "end-session",
            "--session",
            sid,
            "--group-id",
            "12",
        ],
        [
            "browserwright-daemon",
            "kill-executor",
            "--session",
            sid,
        ],
    ]
    assert "still running" in message
    assert reg.get(sid) is None


def test_session_create_end_attach_rdp_reaps_executor(tmp_bs_home, monkeypatch):
    """Failure #3 hardening: an attach-owned rdp session's `end()` leaves the
    browser running (semantics unchanged) but still best-effort reaps the
    session's resident executor (`kill-executor`) so it doesn't leak — the full
    `endSession` path is create-only and never otherwise contacts the daemon."""
    from browserwright import session_create, session_registry as reg

    sid = reg.allocate(backend="rdp", owner="attach", name="attached")
    calls = []
    monkeypatch.setattr(session_create, "_run", lambda cmd: calls.append(cmd) or 0)
    # The browser must NOT be closed for an attach session.
    monkeypatch.setattr(session_create, "_close_browser",
                        lambda rec: calls.append(["CLOSE_BROWSER", rec["id"]]))

    message = session_create.end(reg.get(sid))

    assert calls == [["browserwright-daemon", "kill-executor", "--session", sid]]
    assert "still running" in message  # browser untouched (semantics preserved)
    assert reg.get(sid) is None


def test_session_create_end_create_rdp_does_not_double_reap(tmp_bs_home, monkeypatch):
    """A create-owned session's `end()` drives `_close_browser` (→ daemon
    `endSession`, which ALSO kills the executor), so it must NOT additionally
    call `kill-executor` (no redundant reap)."""
    from browserwright import session_create, session_registry as reg

    sid = reg.allocate(backend="rdp", owner="create", name="owned",
                       workspace={"port": 12345})
    calls = []
    monkeypatch.setattr(session_create, "_run", lambda cmd: calls.append(cmd) or 0)

    message = session_create.end(reg.get(sid))

    # Only the create-owned browser teardown (end-session); no kill-executor.
    assert calls == [["browserwright-daemon", "end-session", "--session", sid]]
    assert "was closed" in message
    assert reg.get(sid) is None


def test_session_create_end_reap_tolerates_dead_daemon(tmp_bs_home, monkeypatch):
    """The executor reap is best-effort: a dead daemon (subprocess error) must
    NOT fail `session end` — `_reap_executor` swallows it via `_run`."""
    from browserwright import session_create, session_registry as reg

    sid = reg.allocate(backend="rdp", owner="attach", name="attached")

    def boom(*_a, **_k):
        raise subprocess.TimeoutExpired("browserwright-daemon", timeout=10)

    monkeypatch.setattr(session_create.subprocess, "run", boom)

    # Must not raise even though the daemon is unreachable.
    message = session_create.end(reg.get(sid))
    assert "still running" in message
    assert reg.get(sid) is None


def test_session_create_new_env_is_attach_shared_context(tmp_bs_home, monkeypatch):
    """issue #20: `session new --backend=env` allocates an attach-owned,
    workspace-less session. attach-owned so end()/reap never close the external
    browser; workspace=None so it routes to the daemon's shared env upstream
    (BD_CDP_WS), not a per-session UpstreamContext."""
    from browserwright import session_create, session_registry as reg

    monkeypatch.setattr(session_create, "_ensure_daemon_running", lambda: None)

    sid = session_create.new(backend="env", name="cloak")
    rec = reg.get(sid)
    assert rec["backend"] == "env"
    assert rec["owner"] == "attach"
    assert rec["workspace"] is None
    assert rec["name"] == "cloak"


def test_session_create_end_env_leaves_external_browser(tmp_bs_home, monkeypatch):
    """An env session is attach-owned: end() reaps only the resident executor
    (kill-executor) and leaves the externally-owned browser running (issue
    #20) — never _close_browser."""
    from browserwright import session_create, session_registry as reg

    sid = reg.allocate(backend="env", owner="attach", name="cloak")
    calls = []
    monkeypatch.setattr(session_create, "_run", lambda cmd: calls.append(cmd) or 0)
    # The external browser must NOT be closed for an env (attach) session.
    monkeypatch.setattr(session_create, "_close_browser",
                        lambda rec: calls.append(["CLOSE_BROWSER", rec["id"]]))

    message = session_create.end(reg.get(sid))

    assert calls == [["browserwright-daemon", "kill-executor", "--session", sid]]
    assert "still running" in message
    assert reg.get(sid) is None


def test_session_registry_unique_name_conflict_does_not_advance_ledger(tmp_bs_home):
    from browserwright import session_registry as reg

    first = reg.allocate(backend="rdp", owner="create", name="job", unique_name=True)
    with pytest.raises(ValueError):
        reg.allocate(backend="extension", owner="attach", name="job", unique_name=True)
    second = reg.allocate(backend="extension", owner="attach", name="other", unique_name=True)

    assert first == "1"
    assert second == "2"
    assert [row["name"] for row in reg.list_all()] == ["job", "other"]


def test_session_registry_backend_immutability_leaves_record_unchanged(tmp_bs_home):
    from browserwright import session_registry as reg

    sid = reg.allocate(backend="extension", owner="attach", name="job")
    assert reg.update(sid, backend="extension", runtime={"ok": True})["runtime"] == {"ok": True}

    with pytest.raises(ValueError):
        reg.update(sid, backend="rdp", owner="create")

    rec = reg.get(sid)
    assert rec["backend"] == "extension"
    assert rec["owner"] == "attach"


def test_task_runner_validate_args_defaults_and_required():
    from browserwright import task_runner

    assert task_runner._validate_args(
        {"query": "news"},
        {"query": {"required": True}, "limit": {"default": 10}},
    ) == {"query": "news", "limit": 10}

    with pytest.raises(ValueError, match="missing required arg: query"):
        task_runner._validate_args({}, {"query": {"required": True}})


def test_task_runner_rejects_module_without_run(tmp_path, monkeypatch):
    from browserwright import task_runner

    task_file = tmp_path / "task_without_run.py"
    task_file.write_text("ARGS = {}\n", encoding="utf-8")
    monkeypatch.setattr(task_runner, "find_task_path", lambda site, name: task_file)

    with pytest.raises(ValueError, match="task module has no run"):
        task_runner.run_task("site", "missing")


def test_task_runner_uses_empty_memory_when_site_memory_read_fails(tmp_path, monkeypatch):
    from browserwright import memory, task_runner

    task_file = tmp_path / "task.py"
    task_file.write_text(
        "ARGS = {'limit': {'default': 2}}\n"
        "def run(args, ctx=None):\n"
        "    return {'args': args, 'memory': ctx.memory}\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(task_runner, "find_task_path", lambda site, name: task_file)
    monkeypatch.setattr(
        memory,
        "site_memory",
        lambda site: (_ for _ in ()).throw(RuntimeError("memory offline")),
    )

    assert task_runner.run_task("site", "ok") == {
        "args": {"limit": 2},
        "memory": {},
    }


def test_task_runner_isolated_session_closes_after_run(tmp_path, monkeypatch):
    from browserwright import session as session_mod, task_runner

    task_file = tmp_path / "task.py"
    task_file.write_text("ARGS = {}\ndef run(args, ctx=None): return 'ok'\n", encoding="utf-8")
    monkeypatch.setattr(task_runner, "find_task_path", lambda site, name: task_file)
    events = []

    class FakeSession:
        def close(self):
            events.append("close")

    fake_session = FakeSession()

    class FakeContext:
        def __enter__(self):
            events.append("enter")
            return fake_session

        def __exit__(self, *exc):
            events.append("exit")

    monkeypatch.setattr(session_mod, "isolated_session", lambda: fake_session)
    monkeypatch.setattr(session_mod, "with_session", lambda sess: FakeContext())
    monkeypatch.setattr(
        "browserwright.session_runtime.open_session_tab",
        lambda sess, url: events.append(("open", sess, url)),
    )

    assert task_runner.run_task("site", "ok", isolated=True) == "ok"
    assert events[0] == "enter"
    assert events[1][0] == "open"
    assert events[1][1] is fake_session
    assert events[1][2].startswith("data:text/html;charset=utf-8,<title>browserwright-isolated-")
    assert events[2:] == ["exit", "close"]


class _RuntimeCDP:
    def __init__(self, *, attach_raises=False, recover_response=None):
        self.calls = []
        self.attach_raises = attach_raises
        self.recover_response = recover_response or {}
        self._sessions = {}
        self._events = {}

    def attach(self, target_id):
        self.calls.append(("attach", target_id))
        if self.attach_raises:
            from browserwright.errors import CDPError

            raise CDPError(method="Target.attachToTarget", cdp_message="gone")
        self._sessions[target_id] = "attached"

    def send(self, method, **params):
        self.calls.append((method, params))
        return self.recover_response


class _RuntimeSession:
    def __init__(self, *, cdp=None, record=None, current_target_id=None):
        self.cdp = cdp or _RuntimeCDP()
        self.session_record = record
        self.current_target_id = current_target_id


def test_session_runtime_ensure_returns_existing_target_without_ledger(tmp_bs_home):
    from browserwright import session_runtime

    sess = _RuntimeSession(current_target_id="tab-existing")

    assert session_runtime.ensure_session_target(sess) == "tab-existing"
    assert sess.cdp.calls == []


def test_session_runtime_stale_fast_path_without_group_returns_none(tmp_bs_home):
    from browserwright import session_registry as reg, session_runtime

    sid = reg.allocate(backend="extension", owner="attach", name="stale")
    reg.update(sid, runtime={"current_target_id": "closed-tab"})
    sess = _RuntimeSession(cdp=_RuntimeCDP(attach_raises=True), record=reg.get(sid))

    assert session_runtime.ensure_session_target(sess) is None
    assert sess.current_target_id is None


def test_session_runtime_register_recovered_rejects_malformed_payload():
    from browserwright import session_runtime

    sess = _RuntimeSession()

    assert session_runtime.register_recovered(sess, {"targetId": "tab-only"}) is None
    assert sess.cdp._sessions == {}


def test_session_runtime_persist_target_swallows_registry_update_failure(tmp_bs_home, monkeypatch):
    from browserwright import session_registry as reg, session_runtime

    sid = reg.allocate(backend="extension", owner="attach", name="persist")
    monkeypatch.setattr(
        reg,
        "update",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("locked")),
    )

    session_runtime.persist_target("tab-1", group_id=1, sess=_RuntimeSession(record={"id": sid}))

    assert "runtime" not in reg.get(sid)


def test_session_decisions_record_creates_home_and_overwrites(tmp_path, monkeypatch):
    from browserwright.memory import session_decisions

    home = tmp_path / "missing-home"
    monkeypatch.setenv("BS_HOME", str(home))

    session_decisions.record("daily scrape", {"backend": "rdp", "mode": "create"})
    session_decisions.record("daily scrape", {"backend": "extension", "mode": "attach"})

    assert home.is_dir()
    assert session_decisions.lookup("daily scrape") == {
        "backend": "extension",
        "mode": "attach",
    }
