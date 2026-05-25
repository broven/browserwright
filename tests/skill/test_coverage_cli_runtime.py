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


def test_cmd_version_check_json_reports_consistent_versions(capsys):
    from browserwright import cli

    assert cli._cmd_version(["check", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
    assert payload["version"] == payload["extension_version"]
    assert payload["extension_protocol_version"] == "1"


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
        ([], "usage: browserwright task"),
        (["missing-slash"], "task spec must be"),
    ],
)
def test_cmd_task_rejects_bad_invocation(args, err, capsys):
    from browserwright import cli

    assert cli._cmd_task(args) == 1
    assert err in capsys.readouterr().err


def test_cmd_task_reports_missing_task(monkeypatch, capsys):
    from browserwright import cli, task_runner

    def missing(site, name, **kwargs):
        raise FileNotFoundError(f"{site}/{name}")

    monkeypatch.setattr(task_runner, "run_task", missing)

    assert cli._cmd_task(["site/name"]) == 1
    assert "task not found" in capsys.readouterr().err


def test_cmd_task_reports_crash(monkeypatch, capsys):
    from browserwright import cli, task_runner

    def crashed(site, name, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(task_runner, "run_task", crashed)

    assert cli._cmd_task(["site/name"]) == 3
    assert "task crashed" in capsys.readouterr().err


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
        ([], "usage: browserwright sub"),
        (["add"], "usage: browserwright sub add"),
        (["remove"], "usage: browserwright sub remove"),
        (["bogus"], "unknown sub subcommand"),
    ],
)
def test_cmd_sub_usage_errors(args, expected, capsys):
    from browserwright import cli

    assert cli._cmd_sub(args) == 1
    assert expected in capsys.readouterr().err


def test_cmd_sub_surfaces_subscription_errors(monkeypatch, capsys):
    from browserwright import cli, subscriptions

    monkeypatch.setattr(
        subscriptions,
        "add",
        lambda *a, **k: (_ for _ in ()).throw(subscriptions.SubscriptionError("bad clone")),
    )
    assert cli._cmd_sub(["add", "https://example.test/repo.git"]) == 1
    assert "sub add failed: bad clone" in capsys.readouterr().err

    monkeypatch.setattr(
        subscriptions,
        "update",
        lambda *a, **k: (_ for _ in ()).throw(subscriptions.SubscriptionError("no git")),
    )
    assert cli._cmd_sub(["update"]) == 1
    assert "sub update failed: no git" in capsys.readouterr().err

    monkeypatch.setattr(
        subscriptions,
        "remove",
        lambda *a, **k: (_ for _ in ()).throw(subscriptions.SubscriptionError("missing")),
    )
    assert cli._cmd_sub(["remove", "--name=old"]) == 1
    assert "sub remove failed: missing" in capsys.readouterr().err


def test_cmd_sub_update_error_status_returns_nonzero(monkeypatch, capsys):
    from browserwright import cli, subscriptions

    monkeypatch.setattr(
        subscriptions,
        "update",
        lambda names=None: [{"name": "starter", "status": "error", "detail": "conflict"}],
    )

    assert cli._cmd_sub(["update", "--name=starter"]) == 1
    assert "conflict" in capsys.readouterr().out


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
    assert "--backend=<extension|rdp>" in capsys.readouterr().err

    assert cli._cmd_session(["bogus"]) == 1
    assert "unknown session subcommand" in capsys.readouterr().err


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


def test_session_create_run_returns_one_for_spawn_errors(monkeypatch):
    from browserwright import session_create

    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired(args[0], timeout=10)

    monkeypatch.setattr(session_create.subprocess, "run", timeout)

    assert session_create._run(["browserwright-daemon", "end-session"]) == 1


def test_session_create_reap_closes_only_create_owned_sessions(tmp_bs_home, monkeypatch):
    from browserwright import session_create, session_registry as reg

    create_sid = reg.allocate(backend="rdp", owner="create", name="owned")
    attach_sid = reg.allocate(backend="rdp", owner="attach", name="attached")
    reg._with_entry(create_sid, lambda e: e.update(last_seen=0.0))
    reg._with_entry(attach_sid, lambda e: e.update(last_seen=0.0))
    closed = []
    monkeypatch.setattr(session_create, "_close_browser", lambda rec: closed.append(rec["id"]))

    pruned = session_create.reap(idle_seconds=1)

    assert {rec["id"] for rec in pruned} == {create_sid, attach_sid}
    assert closed == [create_sid]


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


def test_subscriptions_load_metadata_recovers_from_invalid_json(tmp_bs_home):
    from browserwright import subscriptions

    subscriptions.metadata_path().parent.mkdir(parents=True)
    subscriptions.metadata_path().write_text("{bad json", encoding="utf-8")

    assert subscriptions._load_metadata() == {"version": 1, "subs": {}}
    assert subscriptions.list_all() == []


def test_subscriptions_add_clone_failure_removes_partial_target(tmp_bs_home, monkeypatch):
    from browserwright import subscriptions

    monkeypatch.setattr(subscriptions, "_git_available", lambda: True)

    def failed_clone(argv, **kwargs):
        target = argv[-1]
        subscriptions.Path(target).mkdir(parents=True)
        return SimpleNamespace(returncode=9, stdout="", stderr="network down")

    monkeypatch.setattr(subscriptions.subprocess, "run", failed_clone)

    with pytest.raises(subscriptions.SubscriptionError) as exc:
        subscriptions.add("https://example.test/repo.git", name="repo")

    assert "git clone failed" in str(exc.value)
    assert not (tmp_bs_home / "subscriptions" / "repo").exists()


def test_subscriptions_update_missing_subscription_is_nonfatal(tmp_bs_home, monkeypatch):
    from browserwright import subscriptions

    monkeypatch.setattr(subscriptions, "_git_available", lambda: True)
    subscriptions._save_metadata({"version": 1, "subs": {"gone": {"url": "u"}}})

    assert subscriptions.update(["gone"]) == [{"name": "gone", "status": "missing"}]


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

    assert task_runner.run_task("site", "ok", isolated=True) == "ok"
    assert events == ["enter", "exit", "close"]


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
