"""End-to-end CLI tests via subprocess (verifies the entrypoint actually wires up)."""
import json
import os
import shutil
import subprocess
import sys
from types import SimpleNamespace
from pathlib import Path

import pytest


def _bin() -> str:
    # Prefer a console-script that's actually on PATH; fall back to invoking
    # the CLI as ``python -m browserwright`` so tests are robust against
    # the venv's bin/ not being on PATH.
    found = shutil.which("browserwright")
    if found:
        return found
    return None


def _run(args, *, env=None, input_text=None, cwd=None):
    full_env = os.environ.copy()
    if env:
        full_env.update(env)
    binpath = _bin()
    if binpath is not None:
        cmd = [binpath, *args]
    else:
        cmd = [sys.executable, "-m", "browserwright", *args]
    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        env=full_env,
        input=input_text,
        cwd=cwd,
        timeout=15,
    )


def test_version():
    r = _run(["version"])
    assert r.returncode == 0
    assert r.stdout.strip()


def test_help():
    r = _run(["--help"])
    assert r.returncode == 0
    assert "browserwright" in r.stdout


def test_doctor_runs_without_daemon(tmp_path):
    r = _run(
        ["doctor", "--json"],
        env={
            "BS_HOME": str(tmp_path),
            # Point at a binary that doesn't exist so we hit the synthetic path.
            "PATH": "/usr/bin:/bin",
        },
    )
    # A4: doctor is now CI-style — a missing daemon is a hard fail, so the
    # exit code is nonzero and the body is the {status,message,fix} table.
    assert r.returncode != 0
    info = json.loads(r.stdout)
    assert info["schema_version"] == 1
    assert "skill_version" in info
    assert "checks" in info
    fails = [c for c in info["checks"] if c["status"] == "fail"]
    assert fails
    assert all(c["fix"].strip() for c in fails)


def test_list_tasks_smoke(tmp_path):
    r = _run(
        ["list-tasks", "--json=true"],
        env={"BS_HOME": str(tmp_path)},
        cwd=str(tmp_path),
    )
    assert r.returncode == 0
    # Just make sure the JSON parses — bundled site-skills are inside the
    # checkout so depending on cwd we may or may not see them.
    json.loads(r.stdout)


def test_memory_show_global(tmp_path):
    r = _run(
        ["memory", "show", "--global=true"],
        env={"BS_HOME": str(tmp_path)},
    )
    assert r.returncode == 0
    data = json.loads(r.stdout)
    assert "frontmatter" in data


# ---- Phase 2: session subcommands + whoami (in-process) ----------------

def _main(argv) -> int:
    """Call cli.main and capture the SystemExit code (main always exits)."""
    from browserwright import cli
    try:
        cli.main(argv)
    except SystemExit as e:
        return int(e.code) if isinstance(e.code, int) else 0
    return 0


def test_session_new_extension_registers_attach(tmp_bs_home, capsys):
    from browserwright import session_registry as reg

    rc = _main(["session", "new", "--backend=extension", "--name=research"])
    out = capsys.readouterr().out
    assert rc == 0
    assert out.strip() == "1"  # bare id, token-frugal
    rows = reg.list_all()
    assert len(rows) == 1
    assert rows[0]["backend"] == "extension"
    assert rows[0]["owner"] == "attach"
    assert rows[0]["name"] == "research"


def test_session_new_extension_requires_name(tmp_bs_home, capsys):
    from browserwright import session_registry as reg

    rc = _main(["session", "new", "--backend=extension"])
    err = capsys.readouterr().err
    assert rc == 1
    assert "--name" in err
    assert reg.list_all() == []  # nothing persisted on rejection


def test_session_new_duplicate_name_allowed(tmp_bs_home, capsys):
    """Names are the Chrome tab-group TITLE only — not unique (the session is
    bound internally by its numeric groupId). Two sessions may share a name."""
    from browserwright import session_registry as reg

    rc = _main(["session", "new", "--backend=extension", "--name=dup"])
    first = capsys.readouterr().out.strip()
    assert rc == 0

    rc = _main(["session", "new", "--backend=extension", "--name=dup"])
    second = capsys.readouterr().out.strip()
    assert rc == 0
    assert second != first
    # both sessions exist in the ledger, sharing the name
    rows = reg.list_all()
    assert len(rows) == 2
    assert {r["name"] for r in rows} == {"dup"}


def test_session_new_rdp_create_vs_attach(tmp_bs_home, capsys, monkeypatch):
    from browserwright import session_create
    from browserwright import session_registry as reg

    # P3: the daemon (not session_create) launches/owns the per-session rdp
    # Chrome on ensureSession. session_create just makes sure the one global
    # daemon is up — stub that out so the test needs no real daemon.
    monkeypatch.setattr(session_create, "_ensure_daemon_running", lambda *a, **k: None)

    rc = _main(["session", "new", "--backend=rdp", "--create", "--name=cr"])
    sid_create = capsys.readouterr().out.strip()
    assert rc == 0
    assert reg.get(sid_create)["owner"] == "create"
    # No per-session daemon_endpoint anymore (single global daemon).
    assert "daemon_endpoint" not in reg.get(sid_create)

    rc = _main(["session", "new", "--backend=rdp", "--attach=9222", "--name=at"])
    sid_attach = capsys.readouterr().out.strip()
    assert rc == 0
    assert reg.get(sid_attach)["owner"] == "attach"
    # P3: --attach records both the resolved port (for the daemon to pin to)
    # and the original target.
    assert reg.get(sid_attach)["workspace"] == {"port": 9222, "target": 9222}


def test_session_end_create_closes(tmp_bs_home, capsys, monkeypatch):
    from browserwright import session_create
    from browserwright import session_registry as reg

    closed = []
    monkeypatch.setattr(session_create, "_close_browser", lambda rec: closed.append(rec["id"]))
    sid = reg.allocate(backend="rdp", owner="create", name="job")
    rc = _main(["session", "end", f"--session={sid}"])
    assert rc == 0
    assert closed == [sid]
    assert reg.get(sid) is None


def test_session_end_attach_emits_reminder(tmp_bs_home, capsys):
    from browserwright import session_registry as reg

    sid = reg.allocate(backend="rdp", owner="attach", name="fp")
    rc = _main(["session", "end", f"--session={sid}"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "still running" in out.lower()
    assert reg.get(sid) is None


def test_session_list_and_prune(tmp_bs_home, capsys):
    from browserwright import session_registry as reg

    a = reg.allocate(backend="extension", owner="attach", name="a")
    reg.allocate(backend="rdp", owner="create", name="b")
    rc = _main(["session", "list"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "a" in out and "b" in out

    # make the first ancient, then prune
    reg._with_entry(a, lambda e: e.update(last_seen=0.0))
    rc = _main(["session", "prune", "--idle=3600"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "1" in out  # reported one pruned
    assert reg.get(a) is None


def test_whoami_prints_ledger_view(tmp_bs_home, capsys):
    from browserwright import session_registry as reg

    sid = reg.allocate(backend="rdp",
                       owner="create", name="job")
    rc = _main(["whoami", f"--session={sid}"])
    out = capsys.readouterr().out
    assert rc == 0
    data = json.loads(out)
    assert data["id"] == sid
    assert data["backend"] == "rdp"
    assert data["owner"] == "create"
    assert data["name"] == "job"
    # daemon_endpoint was dropped from the ledger (single global daemon).
    assert "daemon_endpoint" not in data


def test_whoami_unknown_session_refuses(tmp_bs_home, capsys):
    rc = _main(["whoami", "--session=999"])
    assert rc == 2  # NoSession exit code


# --- regression: list-tasks --query (harvested from evals/feedback) ----------
# A real session ran `browserwright list-tasks --query "hacker news"` (the space
# form that `--help` advertises as `[--query Q]`) and got
# `AttributeError: 'bool' object has no attribute 'lower'`: _parse_kv_args stores
# a bare `--query` as True, drops the value, and discovery.score does
# `(query or "").lower()` -> True.lower().

def test_list_tasks_query_eq_form_ok(tmp_bs_home):
    """The `--query=VALUE` form (what the parser accepts) must not crash."""
    rc = _main(["list-tasks", "--query=hacker news"])
    assert rc == 0


def test_list_tasks_query_space_form_should_not_crash(tmp_bs_home):
    """`--query VALUE` (space form, as --help advertises it) must not crash —
    _parse_kv_args now consumes the next token as the value. Regression for the
    `'bool' object has no attribute 'lower'` crash harvested from evals/feedback."""
    rc = _main(["list-tasks", "--query", "hacker news"])
    assert rc == 0


def test_cli_core_command_contracts(monkeypatch, capsys, tmp_bs_home):
    """Exercise the CLI dispatch layer without subprocess/daemon cost."""
    from browserwright import cli
    from browserwright import discovery, health, skill_doc, subscriptions, task_runner

    monkeypatch.setattr(
        health,
        "doctor_checks",
        lambda: {"schema_version": 1, "checks": [
            {"name": "daemon", "status": "warn", "message": "off", "fix": "start it"}
        ]},
    )
    assert cli._cmd_doctor(["--json"]) == 0
    assert json.loads(capsys.readouterr().out)["checks"][0]["fix"] == "start it"

    seen = {}

    def _run_task(site, name, **kwargs):
        seen.update(site=site, name=name, kwargs=kwargs)
        return {"ok": True, "count": kwargs["count"]}

    monkeypatch.setattr(task_runner, "run_task", _run_task)
    assert cli._cmd_task([
        "news/smoke",
        "--count=2",
        "--json-args={\"mode\":\"fast\"}",
        "--json-output",
    ]) == 0
    assert seen == {
        "site": "news",
        "name": "smoke",
        "kwargs": {"count": 2, "mode": "fast", "json-output": True},
    }
    assert json.loads(capsys.readouterr().out) == {"ok": True, "count": 2}

    monkeypatch.setattr(
        discovery,
        "list_tasks",
        lambda **kw: [{"site": kw["site"], "name": "read", "desc": kw["query"]}],
    )
    assert cli._cmd_list_tasks(["--site", "hn", "--query=frontpage", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)[0] == {
        "site": "hn",
        "name": "read",
        "desc": "frontpage",
    }

    monkeypatch.setattr(discovery, "rebuild_index", lambda: {"sites": ["a", "b"]})
    assert cli._cmd_index(["rebuild"]) == 0
    assert json.loads(capsys.readouterr().out) == {"sites": 2}

    monkeypatch.setattr(skill_doc, "render", lambda: "SKILL DOC")
    assert cli._cmd_print_skill([]) == 0
    assert capsys.readouterr().out == "SKILL DOC"

    monkeypatch.setattr(
        subscriptions,
        "add",
        lambda url, name=None: {"status": "added", "name": name, "path": "/tmp/sub"},
    )
    monkeypatch.setattr(
        subscriptions,
        "list_all",
        lambda: [{"name": "starter", "url": "https://example.test/repo.git", "exists": True}],
    )
    monkeypatch.setattr(
        subscriptions,
        "update",
        lambda names=None: [{"name": names[0], "status": "updated", "detail": "ok"}],
    )
    removed = []
    monkeypatch.setattr(subscriptions, "remove", lambda name: removed.append(name))
    assert cli._cmd_sub(["add", "https://example.test/repo.git", "--name=starter", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "added"
    assert cli._cmd_sub(["list", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)[0]["name"] == "starter"
    assert cli._cmd_sub(["update", "--name=starter"]) == 0
    assert "updated" in capsys.readouterr().out
    assert cli._cmd_sub(["remove", "--name=starter"]) == 0
    assert removed == ["starter"]
    assert "removed starter" in capsys.readouterr().out

    assert cli._cmd_memory(["show", "--global"]) == 0
    assert "frontmatter" in json.loads(capsys.readouterr().out)
    assert cli._cmd_memory(["forget", "--pattern=missing", "--global"]) == 0
    assert "(no matching bullets)" in capsys.readouterr().out

    pushed = []
    monkeypatch.setattr(
        cli.subprocess,
        "run",
        lambda argv: pushed.append(argv) or SimpleNamespace(returncode=0),
    )
    monkeypatch.setitem(sys.modules, "browserwright.api", SimpleNamespace(
        reload=lambda: None,
        capture_screenshot=lambda: "/tmp/shot.png",
    ))
    assert cli._cmd_userscript(["push", "script.js", "--verify"]) == 0
    assert pushed == [["browserwright-daemon", "userscript", "push", "script.js"]]
    assert "/tmp/shot.png" in capsys.readouterr().out
