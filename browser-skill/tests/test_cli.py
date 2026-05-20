"""End-to-end CLI tests via subprocess (verifies the entrypoint actually wires up)."""
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest


def _bin() -> str:
    # Prefer a console-script that's actually on PATH; fall back to invoking
    # the CLI as ``python -m browser_skill.cli`` so tests are robust against
    # the venv's bin/ not being on PATH.
    found = shutil.which("browser-skill")
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
        cmd = [sys.executable, "-m", "browser_skill.cli", *args]
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
    assert "browser-skill" in r.stdout


def test_doctor_runs_without_daemon(tmp_path):
    r = _run(
        ["doctor", "--json"],
        env={
            "BS_HOME": str(tmp_path),
            # Point at a binary that doesn't exist so we hit the synthetic path.
            "PATH": "/usr/bin:/bin",
        },
    )
    assert r.returncode == 0
    info = json.loads(r.stdout)
    assert info["schema_version"] == 1
    assert "skill_version" in info


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
    from browser_skill import cli
    try:
        cli.main(argv)
    except SystemExit as e:
        return int(e.code) if isinstance(e.code, int) else 0
    return 0


def test_session_new_extension_registers_attach(tmp_bs_home, capsys):
    from browser_skill import session_registry as reg

    rc = _main(["session", "new", "--backend=extension", "--name=research"])
    out = capsys.readouterr().out
    assert rc == 0
    assert out.strip() == "1"  # bare id, token-frugal
    rows = reg.list_all()
    assert len(rows) == 1
    assert rows[0]["backend"] == "extension"
    assert rows[0]["owner"] == "attach"
    assert rows[0]["name"] == "research"


def test_session_new_rdp_create_vs_attach(tmp_bs_home, capsys, monkeypatch):
    from browser_skill import session_create
    from browser_skill import session_registry as reg

    monkeypatch.setattr(session_create, "_launch_daemon", lambda *a, **k: None)

    rc = _main(["session", "new", "--backend=rdp", "--create"])
    sid_create = capsys.readouterr().out.strip()
    assert rc == 0
    assert reg.get(sid_create)["owner"] == "create"
    assert reg.get(sid_create)["daemon_endpoint"] == f"browser-daemon-s{sid_create}"

    rc = _main(["session", "new", "--backend=rdp", "--attach=9222"])
    sid_attach = capsys.readouterr().out.strip()
    assert rc == 0
    assert reg.get(sid_attach)["owner"] == "attach"
    assert reg.get(sid_attach)["workspace"] == {"target": 9222}


def test_session_end_create_closes(tmp_bs_home, capsys, monkeypatch):
    from browser_skill import session_create
    from browser_skill import session_registry as reg

    closed = []
    monkeypatch.setattr(session_create, "_close_browser", lambda rec: closed.append(rec["id"]))
    sid = reg.allocate(backend="rdp", daemon_endpoint="d", owner="create", name="job")
    rc = _main(["session", "end", f"--session={sid}"])
    assert rc == 0
    assert closed == [sid]
    assert reg.get(sid) is None


def test_session_end_attach_emits_reminder(tmp_bs_home, capsys):
    from browser_skill import session_registry as reg

    sid = reg.allocate(backend="rdp", daemon_endpoint="d", owner="attach", name="fp")
    rc = _main(["session", "end", f"--session={sid}"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "still running" in out.lower()
    assert reg.get(sid) is None


def test_session_list_and_prune(tmp_bs_home, capsys):
    from browser_skill import session_registry as reg

    a = reg.allocate(backend="extension", daemon_endpoint="default", owner="attach", name="a")
    reg.allocate(backend="rdp", daemon_endpoint="d", owner="create", name="b")
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
    from browser_skill import session_registry as reg

    sid = reg.allocate(backend="rdp", daemon_endpoint="browser-daemon-s1",
                       owner="create", name="job")
    rc = _main(["whoami", f"--session={sid}"])
    out = capsys.readouterr().out
    assert rc == 0
    data = json.loads(out)
    assert data["id"] == sid
    assert data["backend"] == "rdp"
    assert data["owner"] == "create"
    assert data["name"] == "job"
    assert data["daemon_endpoint"] == "browser-daemon-s1"


def test_whoami_unknown_session_refuses(tmp_bs_home, capsys):
    rc = _main(["whoami", "--session=999"])
    assert rc == 2  # NoSession exit code
