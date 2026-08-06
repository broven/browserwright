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

    assert cli._parse_execute_args([
        "-s",
        "1",
        "--env",
        "USPS_EMAIL",
        "--env=USPS_PASSWORD",
        "-e",
        "print(1)",
    ]) == (
        "1",
        "print(1)",
        ["USPS_EMAIL", "USPS_PASSWORD"],
        None,
    )
    assert cli._parse_execute_args(["--session=2", "--execute=print(2)"]) == (
        "2",
        "print(2)",
        [],
        None,
    )
    assert cli._parse_execute_args(["-s", "1"])[3] == (
        "missing code: pass -e '<python>', -f <path>, or --code-stdin"
    )


def test_parse_execute_args_reads_code_file(tmp_path):
    from browserwright import cli

    script = tmp_path / "script.py"
    script.write_text("print('from file')\n")

    assert cli._parse_execute_args(["-s", "1", "-f", str(script)]) == (
        "1",
        "print('from file')\n",
        [],
        None,
    )


def test_parse_execute_args_rejects_multiple_code_sources():
    from browserwright import cli

    assert cli._parse_execute_args([
        "-s", "1", "-e", "print(1)", "--code-stdin",
    ])[3] == (
        "pass only one of -e, -f, or --code-stdin"
    )


def test_parse_execute_args_reads_code_stdin(monkeypatch):
    from io import StringIO
    from browserwright import cli

    monkeypatch.setattr(cli.sys, "stdin", StringIO("print('stdin')\n"))

    assert cli._parse_execute_args(["--session=1", "--code-stdin"]) == (
        "1",
        "print('stdin')\n",
        [],
        None,
    )


@pytest.mark.parametrize(
    ("args", "expected_error"),
    [
        (["-s", "1", "--env", "-e", "print(1)"],
         "--env requires a variable name"),
        (["-s", "1", "--env=", "-e", "print(1)"],
         "--env requires a variable name"),
        (["-s", "1", "--env", "TOKEN=secret", "-e", "print(1)"],
         "--env 'TOKEN' must not include a value"),
        (["-s", "1", "--env", "1TOKEN", "-e", "print(1)"],
         "invalid environment variable name"),
        (["-s", "1", "--env", "not-valid-secret", "-e", "print(1)"],
         "invalid environment variable name"),
    ],
)
def test_parse_execute_args_rejects_invalid_env_without_echoing_value(
    args, expected_error,
):
    from browserwright import cli

    error = cli._parse_execute_args(args)[3]

    assert expected_error in error
    assert "secret" not in error


def test_cmd_execute_dispatches_selected_env(monkeypatch):
    from browserwright import cli
    from browserwright.repl import inline

    calls = []
    monkeypatch.setattr(
        inline,
        "run_code",
        lambda code, *, session_id, env: calls.append(
            (session_id, code, env)
        ) or 0,
    )
    monkeypatch.setenv("SITE_EMAIL", "alice@example.test")

    assert cli._cmd_execute([
        "-s", "abc", "--env", "SITE_EMAIL", "-e", "print('ok')",
    ]) == 0
    assert calls == [(
        "abc",
        "print('ok')",
        {"SITE_EMAIL": "alice@example.test"},
    )]


def test_cmd_execute_missing_env_reports_name_not_other_values(
    monkeypatch, capsys,
):
    from browserwright import cli

    monkeypatch.delenv("SITE_MISSING", raising=False)
    monkeypatch.setenv("UNRELATED_SECRET", "must-not-leak")

    assert cli._cmd_execute([
        "-s", "abc", "--env", "SITE_MISSING", "-e", "page.url",
    ]) == 1
    error = capsys.readouterr().err
    assert "SITE_MISSING" in error
    assert "must-not-leak" not in error


def test_global_session_prefix_dispatches_execute_starting_with_env(monkeypatch):
    from browserwright import cli

    calls = []
    monkeypatch.setattr(
        cli,
        "_cmd_execute",
        lambda args: calls.append(args) or 0,
    )
    argv = ["-s", "abc", "--env", "SITE_EMAIL", "-e", "page.url"]

    with pytest.raises(SystemExit) as exc:
        cli.main(argv)

    assert exc.value.code == 0
    assert calls == [argv]


def test_global_session_prefix_dispatches_task(monkeypatch, tmp_bs_home, capsys):
    from browserwright import cli
    from browserwright import session_registry as reg
    from browserwright._executor import client as executor_client
    from browserwright._executor.protocol import ExecuteResponse

    sid = reg.allocate(backend="cdp", owner="create", name="job")
    calls = []
    monkeypatch.setattr(
        executor_client,
        "run_task_on_executor",
        lambda sess, site, name, **kw: (
            calls.append((sess, site, name, kw))
            or ExecuteResponse(
                task_result_json=json.dumps({
                    "site": site,
                    "name": name,
                    "kwargs": kw["args"],
                })
            )
        ),
    )

    with pytest.raises(SystemExit) as exc:
        cli.main(["-s", sid, "task", "example.com/check", "--count=2", "--output", "json"])
    assert exc.value.code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload == {
        "site": "example.com",
        "name": "check",
        "kwargs": {"count": 2},
    }
    assert calls[0][3]["isolated"] is False


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


def test_cmd_task_reports_missing_task(monkeypatch, capsys, tmp_bs_home):
    from browserwright import cli
    from browserwright import session_registry as reg
    from browserwright._executor import client as executor_client
    from browserwright._executor.protocol import ExecuteResponse

    monkeypatch.setattr(
        executor_client,
        "run_task_on_executor",
        lambda *_args, **_kwargs: ExecuteResponse(
            error={"type": "FileNotFoundError", "msg": "site/name"},
            exit_code=3,
        ),
    )
    sid = reg.allocate(backend="cdp", owner="create")

    assert cli._cmd_task(["--session", sid, "site/name"]) == 1
    assert "task not found" in capsys.readouterr().err


def test_cmd_task_reports_crash(monkeypatch, capsys, tmp_bs_home):
    from browserwright import cli
    from browserwright import session_registry as reg
    from browserwright._executor import client as executor_client
    from browserwright._executor.protocol import ExecuteResponse

    monkeypatch.setattr(
        executor_client,
        "run_task_on_executor",
        lambda *_args, **_kwargs: ExecuteResponse(
            error={"type": "RuntimeError", "msg": "boom"},
            exit_code=3,
        ),
    )
    sid = reg.allocate(backend="cdp", owner="create")

    assert cli._cmd_task(["--session", sid, "site/name"]) == 3
    assert "task crashed" in capsys.readouterr().err


def test_cmd_task_binds_session_and_outputs_json(monkeypatch, tmp_bs_home, capsys):
    from browserwright import cli
    from browserwright import session_registry as reg
    from browserwright._executor import client as executor_client
    from browserwright._executor.protocol import ExecuteResponse

    sid = reg.allocate(backend="cdp", owner="create")
    captured = {}
    monkeypatch.setattr(
        executor_client,
        "run_task_on_executor",
        lambda sess, site, name, **kwargs: (
            captured.update({
                "session": sess,
                "site": site,
                "name": name,
                **kwargs,
            })
            or ExecuteResponse(
                console="task log\n",
                task_result_json=json.dumps({
                    "site": site,
                    "name": name,
                    "limit": kwargs["args"]["limit"],
                }),
            )
        ),
    )

    assert cli._cmd_task(["--session", sid, "site/name", "--limit=3", "--output", "json"]) == 0
    output = capsys.readouterr().out
    assert output.startswith("task log\n")
    assert json.loads(output.removeprefix("task log\n")) == {
        "site": "site",
        "name": "name",
        "limit": 3,
    }
    assert captured["session"].session_record["id"] == sid
    assert captured["isolated"] is False


def test_cmd_task_default_output_uses_executor_repr_and_isolated(
    monkeypatch, tmp_bs_home, capsys,
):
    from browserwright import cli
    from browserwright import session_registry as reg
    from browserwright._executor import client as executor_client
    from browserwright._executor.protocol import ExecuteResponse

    captured = {}
    monkeypatch.setattr(
        executor_client,
        "run_task_on_executor",
        lambda _sess, _site, _name, **kwargs: (
            captured.update(kwargs)
            or ExecuteResponse(
                return_value="<CustomResult exact-repr>",
                task_result_json='"string fallback is intentionally different"',
            )
        ),
    )
    sid = reg.allocate(backend="cdp", owner="create")

    assert cli._cmd_task(["--session", sid, "site/name", "--isolated"]) == 0
    assert capsys.readouterr().out == "<CustomResult exact-repr>\n"
    assert captured["isolated"] is True
    assert captured["args"] == {}


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


def test_cmd_session_reset_recycles_executor(tmp_bs_home, monkeypatch, capsys):
    from browserwright import cli, session_create, session_registry as reg

    sid = reg.allocate(backend="cdp", owner="attach", name="attached")
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

    sid = reg.allocate(backend="cdp", owner="attach", name="attached")

    with pytest.raises(SystemExit) as exc:
        cli.main(["-s", sid, "whoami"])
    assert exc.value.code == 0
    assert json.loads(capsys.readouterr().out)["id"] == sid


def test_global_session_prefix_dispatches_session_end(tmp_bs_home, monkeypatch, capsys):
    from browserwright import cli, session_create, session_registry as reg

    sid = reg.allocate(backend="cdp", owner="attach", name="attached")
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

    outer = reg.allocate(backend="cdp", owner="attach", name="outer")
    inner = reg.allocate(backend="cdp", owner="attach", name="inner")
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

    sid = reg.allocate(backend="cdp", owner="attach", name="attached")
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

    assert cli._cmd_session(["new", "--backend=cdp", "--name=job"]) == 0
    streams = capsys.readouterr()
    assert streams.out == "17\n"
    assert streams.err == "OK: session 17 created\n"


def test_cmd_session_new_rejects_env_backend_naming_its_replacement(
    monkeypatch, tmp_bs_home, capsys,
):
    """#38: `env` is gone, and the rejection has to say what to write instead.

    Anyone with `--backend=env` in a script also has BD_CDP_WS in their
    environment; "invalid choice" would leave them with no path forward.
    """
    from browserwright import cli, session_create

    called = []
    monkeypatch.setattr(session_create, "new", lambda **kw: called.append(kw) or "7")

    assert cli._cmd_session(["new", "--backend=env", "--name=cloak"]) == 1
    assert called == []  # rejected at the gate, never reaches allocation
    err = capsys.readouterr().err
    assert "--attach=" in err
    assert "BD_CDP_WS" in err


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

    sid = reg.allocate(backend="cdp", owner="create")
    monkeypatch.setenv("BD_SESSION", sid)
    calls = []

    def fake_run(argv, **kwargs):
        calls.append(argv)
        if argv[:3] == ["browserwright-daemon", "status", "--json"]:
            return SimpleNamespace(returncode=1, stdout="", stderr="")
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(cli.subprocess, "run", fake_run)
    monkeypatch.setattr("browserwright.mode_b_client.ModeBClient.is_alive", lambda self: False)

    # --verify is queued on the same resident executor as -e and task.
    executor_calls = []
    monkeypatch.setattr(
        "browserwright._executor.client.run_on_executor",
        lambda sess, code: executor_calls.append((sess, code))
        or SimpleNamespace(error=None, exit_code=0),
    )
    monkeypatch.setattr(
        "browserwright.repl.playwright_handle.PlaywrightHandle",
        lambda: pytest.fail("verify must not create a second Playwright handle"),
    )
    monkeypatch.setattr(cli, "_fresh_screenshot_path", lambda: "/tmp/shot.png")

    assert cli._cmd_userscript(["push", "script.js", "--verify"]) == 0
    assert calls == [["browserwright-daemon", "userscript", "push", "script.js"]]
    assert capsys.readouterr().out == "/tmp/shot.png\n"
    assert len(executor_calls) == 1
    executor_session, code = executor_calls[0]
    assert executor_session.session_record["id"] == sid
    assert "page.reload(wait_until='load')" in code
    assert "page.screenshot(path='/tmp/shot.png')" in code


def test_cmd_userscript_verify_failure_preserves_successful_push(
        monkeypatch, tmp_bs_home, capsys):
    from browserwright import cli
    from browserwright import session_registry as reg

    sid = reg.allocate(backend="cdp", owner="create")
    monkeypatch.setenv("BD_SESSION", sid)
    monkeypatch.setattr(
        cli.subprocess,
        "run",
        lambda argv, **kwargs: SimpleNamespace(
            returncode=0, stdout="", stderr="",
        ),
    )
    monkeypatch.setattr(
        "browserwright._executor.client.run_on_executor",
        lambda sess, code: SimpleNamespace(
            error={"type": "PageBindTimeout", "msg": "target did not appear"},
            exit_code=3,
        ),
    )

    assert cli._cmd_userscript(["push", "script.js", "--verify"]) == 0
    streams = capsys.readouterr()
    assert streams.out == ""
    assert "pushed OK — --verify skipped" in streams.err
    assert "PageBindTimeout: target did not appear" in streams.err


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
    assert by_name["daemon_cli"]["status"] == "pass"
    # No liveness fields in the blob (pre-v3 daemon): we can't verify the
    # daemon is running, so the check warns instead of asserting health.
    assert by_name["daemon_running"]["status"] == "warn"
    assert by_name["daemon_schema"]["status"] == "warn"
    assert by_name["backend"]["status"] == "fail"
    assert by_name["backend"]["fix"]


def test_doctor_checks_no_daemon_is_single_root_cause_failure(monkeypatch):
    """Gate (issue #28): with no daemon running, doctor emits exactly one
    root-cause failure and it names the daemon. Backend/extension
    unavailability are consequences of the down daemon, not independent
    failures — reporting them independently is what misdirected users."""
    from browserwright import health

    monkeypatch.setattr(health, "_launchagent_installed", lambda: False)
    monkeypatch.setattr(health, "daemon_doctor", lambda: {
        "schema_version": 3,
        "alive": False,
        "probe_state": "not_running",
        "pid": None,
        "recommended": None,
        "backends": [
            {"name": "env", "available": False},
            {"name": "cdp", "available": False},
            {"name": "extension", "available": False,
             "needs_user_action": "start the single global daemon, then load "
                                  "the Chrome extension from `chrome-extension/`"},
        ],
    })

    checks = health.doctor_checks()["checks"]
    fails = [c for c in checks if c["status"] == "fail"]
    assert [c["name"] for c in fails] == ["daemon_running"]
    assert "browserwright-daemon serve" in fails[0]["fix"]
    backend = next(c for c in checks if c["name"] == "backend")
    assert backend["status"] == "warn"
    assert "deferred" in backend["message"]


def test_doctor_checks_daemon_down_with_launchagent_fix_says_restart(monkeypatch):
    """A LaunchAgent-managed install restarts rather than serves: a bare
    `serve` would fight launchd over the socket (issue #28)."""
    from browserwright import health

    monkeypatch.setattr(health, "_launchagent_installed", lambda: True)
    monkeypatch.setattr(health, "daemon_doctor", lambda: {
        "schema_version": 3, "alive": False,
        "probe_state": "not_running", "pid": None, "backends": [],
    })

    checks = health.doctor_checks()["checks"]
    running = next(c for c in checks if c["name"] == "daemon_running")
    assert running["status"] == "fail"
    assert "restart" in running["fix"]


def test_doctor_checks_half_alive_daemon_fix_reclaims_ports(monkeypatch):
    """A half-alive daemon (port_held_by_unresponsive_process) always needs
    `restart` to reclaim its ports — never a plain `serve`, which would crash
    on EADDRINUSE (issue #28 / #15)."""
    from browserwright import health

    monkeypatch.setattr(health, "_launchagent_installed", lambda: False)
    monkeypatch.setattr(health, "daemon_doctor", lambda: {
        "schema_version": 3, "alive": False,
        "probe_state": "port_held_by_unresponsive_process", "pid": None,
        "backends": [],
    })

    checks = health.doctor_checks()["checks"]
    running = next(c for c in checks if c["name"] == "daemon_running")
    assert running["status"] == "fail"
    assert "restart" in running["fix"]
    assert "serve" not in running["fix"]


def test_doctor_checks_alive_daemon_passes_running(monkeypatch):
    from browserwright import health

    monkeypatch.setattr(health, "daemon_doctor", lambda: {
        "schema_version": 3, "alive": True, "probe_state": "ok",
        "pid": 4242, "backends": [],
    })

    checks = health.doctor_checks()["checks"]
    by_name = {c["name"]: c for c in checks}
    assert by_name["daemon_running"]["status"] == "pass"
    assert "4242" in by_name["daemon_running"]["message"]


def test_cmd_doctor_no_daemon_reports_daemon_running_as_headline(monkeypatch, capsys):
    """End-to-end gate (issue #28): `browserwright doctor` with no daemon
    running prints the daemon as the one root-cause failure — and never a
    `✗ backend` that reads as an extension-side problem."""
    from browserwright import cli, health

    monkeypatch.setattr(health, "_launchagent_installed", lambda: False)
    monkeypatch.setattr(health, "daemon_doctor", lambda: {
        "schema_version": 3, "alive": False,
        "probe_state": "not_running", "pid": None,
        "backends": [{"name": "extension", "available": False}],
    })

    assert cli._cmd_doctor([]) == 1
    out = capsys.readouterr().out
    assert "✗ daemon_running" in out
    assert "browserwright-daemon serve" in out
    assert "✗ backend" not in out
    assert "doctor: FAIL" in out


def test_session_create_reset_fix_no_longer_loops_through_doctor(monkeypatch):
    """Secondary (issue #28): `session reset`'s fix text used to say 'run
    `browserwright doctor`' — which reported `✓ daemon` on a down daemon, so
    the advice looped. It now names the actual check/start commands. (Issue
    #40: with the daemon down the executor is reaped locally when provably
    gone — so this exercise pins the failure path to the un-reapeable case,
    where the fix text is what the agent actually sees.)"""
    from browserwright import session_create

    sid = "deadbeef"
    monkeypatch.setattr(session_create, "_run", lambda cmd: 1)
    monkeypatch.setattr(session_create, "_ensure_daemon_running", lambda: None)
    monkeypatch.setattr(session_create, "_daemon_is_running", lambda: False)
    monkeypatch.setattr(session_create, "_reap_executor_locally", lambda _sid: None)
    from browserwright.errors import DaemonUnavailable

    with pytest.raises(DaemonUnavailable) as excinfo:
        session_create.reset_executor({"id": sid})
    assert "browserwright doctor" not in excinfo.value.fix
    assert "browserwright-daemon status" in excinfo.value.fix
    assert "browserwright-daemon serve" in excinfo.value.fix
    assert sid in excinfo.value.fix


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


def test_session_create_run_returns_three_for_timeout(monkeypatch):
    """A timed-out end-session subprocess is a failure (exit 3, matching the
    CLI's own TimeoutError mapping), never a crash — the ledger row is kept
    for retry, and the retry joins the daemon-side teardown (issue #32)."""
    from browserwright import session_create

    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired(args[0], timeout=10)

    monkeypatch.setattr(session_create.subprocess, "run", timeout)

    assert session_create._run(["browserwright-daemon", "end-session"]) == 3


def test_session_create_reap_tears_down_before_removing_ledger(tmp_bs_home, monkeypatch):
    from browserwright import session_create, session_registry as reg

    create_sid = reg.allocate(backend="cdp", owner="create", name="owned")
    attach_sid = reg.allocate(backend="cdp", owner="attach", name="attached")
    ext_sid = reg.allocate(backend="extension", owner="attach", name="ext")
    reg._with_entry(create_sid, lambda e: e.update(last_seen=0.0))
    reg._with_entry(attach_sid, lambda e: e.update(last_seen=0.0))
    reg._with_entry(ext_sid, lambda e: e.update(last_seen=0.0))
    ended = []

    def _run(cmd, **kwargs):
        sid = cmd[cmd.index("--session") + 1]
        assert reg.get(sid) is not None
        ended.append(sid)
        return 0

    monkeypatch.setattr(session_create, "_run", _run)

    pruned = session_create.reap(idle_seconds=1)

    assert {rec["id"] for rec in pruned} == {create_sid, attach_sid, ext_sid}
    assert ended == [create_sid, attach_sid, ext_sid]
    assert reg.get(create_sid) is None
    assert reg.get(attach_sid) is None
    assert reg.get(ext_sid) is None


def test_create_owned_end_keeps_ledger_when_daemon_teardown_is_partial(
    tmp_bs_home, monkeypatch,
):
    """The daemon is UP (so #40's daemon-down force-drop does not apply) but
    its teardown comes back partial: the row is kept for the #32 retry."""
    from browserwright import session_create, session_registry as reg
    from browserwright.errors import DaemonUnavailable

    sid = reg.allocate(backend="cdp", owner="create", name="owned")
    record = reg.get(sid)
    monkeypatch.setattr(session_create, "_run", lambda _cmd, **kwargs: 3)
    monkeypatch.setattr(session_create, "_daemon_is_running", lambda: True)

    with pytest.raises(DaemonUnavailable, match="ledger entry was kept"):
        session_create.end(record)
    assert reg.get(sid) is not None


def test_session_create_reset_executor_keeps_ledger(tmp_bs_home, monkeypatch):
    from browserwright import session_create, session_registry as reg

    sid = reg.allocate(backend="cdp", owner="attach", name="attached")
    calls = []
    monkeypatch.setattr(session_create, "_ensure_daemon_running", lambda: calls.append(["ensure"]))
    monkeypatch.setattr(session_create, "_run", lambda cmd, **kwargs: calls.append(cmd) or 0)

    message = session_create.reset_executor(reg.get(sid))

    assert calls == [["ensure"], ["browserwright-daemon", "kill-executor", "--session", sid]]
    assert "left untouched" in message
    assert reg.get(sid) is not None


def test_session_create_reset_executor_refuses_unconfirmed_reap(
    tmp_bs_home, monkeypatch,
):
    """A reset that cannot confirm the executor is gone still refuses — with
    the daemon down this is now the un-reapeable-orphan case (issue #40): the
    local reap came back empty-handed, so the row is kept for retry."""
    from browserwright import session_create, session_registry as reg
    from browserwright.errors import DaemonUnavailable

    sid = reg.allocate(backend="cdp", owner="attach", name="attached")
    monkeypatch.setattr(session_create, "_ensure_daemon_running", lambda: None)
    monkeypatch.setattr(session_create, "_run", lambda _cmd: 1)
    monkeypatch.setattr(session_create, "_daemon_is_running", lambda: False)
    monkeypatch.setattr(session_create, "_reap_executor_locally", lambda _sid: None)

    with pytest.raises(DaemonUnavailable, match="could not confirm"):
        session_create.reset_executor(reg.get(sid))

    assert reg.get(sid) is not None


def test_cmd_session_reset_reports_unconfirmed_reap(
    tmp_bs_home, monkeypatch, capsys,
):
    from browserwright import cli, session_create, session_registry as reg
    from browserwright.errors import DaemonUnavailable

    sid = reg.allocate(backend="cdp", owner="attach", name="attached")
    monkeypatch.setattr(
        session_create,
        "reset_executor",
        lambda _rec: (_ for _ in ()).throw(
            DaemonUnavailable("could not confirm executor reap")
        ),
    )

    assert cli._cmd_session(["reset", sid]) == DaemonUnavailable.exit_code
    assert "could not confirm executor reap" in capsys.readouterr().err
    assert reg.get(sid) is not None


def test_session_create_end_extension_threads_group_id(tmp_bs_home, monkeypatch):
    from browserwright import session_create, session_registry as reg

    sid = reg.allocate(backend="extension", owner="attach", name="shared")
    reg.update(sid, runtime={"group_id": 12})
    calls = []
    monkeypatch.setattr(session_create, "_run", lambda cmd, **kwargs: calls.append(cmd) or 0)

    message = session_create.end(reg.get(sid))

    # The daemon's one terminal lifecycle closes the extension group, reaps the
    # executor, and revokes clients while leaving the browser itself running.
    assert calls == [
        [
            "browserwright-daemon",
            "end-session",
            "--session",
            sid,
            "--group-id",
            "12",
        ],
    ]
    assert "still running" in message
    assert reg.get(sid) is None


def test_cmd_session_end_reports_partial_extension_teardown(
    tmp_bs_home, monkeypatch, capsys,
):
    from browserwright import cli, session_create, session_registry as reg
    from browserwright.errors import DaemonUnavailable

    sid = reg.allocate(backend="extension", owner="attach", name="shared")

    def fail_end(_record):
        raise DaemonUnavailable("extension tabs remain open")

    monkeypatch.setattr(session_create, "end", fail_end)

    assert cli._cmd_session(["end"], session_id=sid) == DaemonUnavailable.exit_code
    assert "extension tabs remain open" in capsys.readouterr().err
    assert reg.get(sid) is not None


def test_session_create_end_attach_cdp_reaps_executor(tmp_bs_home, monkeypatch):
    """Attach cdp uses daemon termination but keeps its external browser."""
    from browserwright import session_create, session_registry as reg

    sid = reg.allocate(backend="cdp", owner="attach", name="attached")
    calls = []
    monkeypatch.setattr(session_create, "_run", lambda cmd, **kwargs: calls.append(cmd) or 0)
    message = session_create.end(reg.get(sid))

    assert calls == [["browserwright-daemon", "end-session", "--session", sid]]
    assert "still running" in message  # browser untouched (semantics preserved)
    assert reg.get(sid) is None


def test_session_create_end_create_cdp_does_not_double_reap(tmp_bs_home, monkeypatch):
    """A create-owned session's `end()` drives `_close_browser` (→ daemon
    `endSession`, which ALSO kills the executor), so it must NOT additionally
    call `kill-executor` (no redundant reap)."""
    from browserwright import session_create, session_registry as reg

    sid = reg.allocate(backend="cdp", owner="create", name="owned",
                       workspace={"port": 12345})
    calls = []
    monkeypatch.setattr(session_create, "_run", lambda cmd, **kwargs: calls.append(cmd) or 0)

    message = session_create.end(reg.get(sid))

    # Only the create-owned browser teardown (end-session); no kill-executor.
    assert calls == [["browserwright-daemon", "end-session", "--session", sid]]
    assert "was closed" in message
    assert reg.get(sid) is None


def test_session_create_end_keeps_attach_ledger_when_daemon_is_unconfirmed(
    tmp_bs_home, monkeypatch,
):
    """An unreachable daemon cannot prove that attach clients were revoked —
    and the #40 local reap could not prove the executor gone either — so the
    row is kept for retry instead of being dropped on a guess."""
    from browserwright import session_create, session_registry as reg
    from browserwright.errors import DaemonUnavailable

    sid = reg.allocate(backend="cdp", owner="attach", name="attached")

    def boom(*_a, **_k):
        raise subprocess.TimeoutExpired("browserwright-daemon", timeout=10)

    monkeypatch.setattr(session_create.subprocess, "run", boom)
    monkeypatch.setattr(session_create, "_daemon_is_running", lambda: False)
    monkeypatch.setattr(session_create, "_reap_executor_locally", lambda _sid: None)

    with pytest.raises(DaemonUnavailable, match="ledger entry was kept"):
        session_create.end(reg.get(sid))
    assert reg.get(sid) is not None


def test_session_registry_backend_immutability_leaves_record_unchanged(tmp_bs_home):
    from browserwright import session_registry as reg

    sid = reg.allocate(backend="extension", owner="attach", name="job")
    assert reg.update(sid, backend="extension", runtime={"ok": True})["runtime"] == {"ok": True}

    with pytest.raises(ValueError):
        reg.update(sid, backend="cdp", owner="create")

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
