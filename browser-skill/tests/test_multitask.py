"""Multi-task fan-out tests (v0.3).

The interesting assertion is that ``run_tasks_concurrent`` actually runs
each spec in its own ``Session`` — i.e. ``current_session()`` differs
between workers — and that the results come back in input order even if
the tasks finish out of order.

We don't actually touch the browser here. Each test plants a synthetic
task that introspects ``current_session()`` / fakes timing without going
through CDP.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _isolated_modules(monkeypatch, tmp_path):
    """Cleared modules + an empty bundled root so each test's planted tasks
    are the only ones discoverable."""
    for k in list(sys.modules):
        if k.startswith("browser_skill"):
            del sys.modules[k]
    empty = tmp_path / "empty-bundle"
    empty.mkdir()
    from browser_skill import discovery
    monkeypatch.setattr(discovery, "_bundled_root", lambda: empty)
    # Stub daemon so Session() never hits the network.
    from browser_skill import session as _sess_mod

    class _StubDaemon:
        def resolve_ws_url(self):
            return "ws://stub/never"

        def invalidate(self):
            pass

    monkeypatch.setattr(
        "browser_skill.mode_b_client.auto_client",
        lambda *_a, **_k: _StubDaemon(),
    )
    yield


def _plant_task(root: Path, site: str, name: str, body: str = 'return "ok"'):
    (root / site / "tasks").mkdir(parents=True, exist_ok=True)
    (root / site / "memory.md").write_text(f"# {site}\n", encoding="utf-8")
    (root / site / "tasks" / f"{name}.py").write_text(
        f'"""{name}"""\nARGS = {{}}\n'
        f'def selftest(): return True\n'
        f'def run(args, ctx=None):\n'
        f'    {body}\n',
        encoding="utf-8",
    )


def test_results_in_input_order(tmp_bs_home):
    """Inputs come back in order even when later tasks finish first."""
    from browser_skill.memory.site_mem import site_skills_root
    from browser_skill.multitask import run_tasks_concurrent
    root = site_skills_root()
    _plant_task(root, "slow", "x", body="import time; time.sleep(0.05); return 'A'")
    _plant_task(root, "fast", "y", body="return 'B'")

    rows = run_tasks_concurrent([("slow", "x", {}), ("fast", "y", {})])
    assert [r["site"] for r in rows] == ["slow", "fast"]
    assert rows[0]["value"] == "A"
    assert rows[1]["value"] == "B"


def test_each_task_has_its_own_session(tmp_bs_home):
    """Two concurrent tasks must observe different ``current_session()``
    object identities."""
    from browser_skill.memory.site_mem import site_skills_root
    from browser_skill.multitask import run_tasks_concurrent
    root = site_skills_root()
    _plant_task(
        root, "ident", "snap",
        body=(
            "from browser_skill.session import current_session\n"
            "    import time; time.sleep(0.02)\n"
            "    return id(current_session())"
        ),
    )

    rows = run_tasks_concurrent(
        [("ident", "snap", {}), ("ident", "snap", {})],
        max_workers=2,
    )
    assert all(r["ok"] for r in rows)
    assert rows[0]["value"] != rows[1]["value"], \
        "two concurrent tasks should see distinct Session ids"


def test_failure_isolated_to_its_own_row(tmp_bs_home):
    """One task raises; the others' results are unaffected."""
    from browser_skill.memory.site_mem import site_skills_root
    from browser_skill.multitask import run_tasks_concurrent
    root = site_skills_root()
    _plant_task(root, "happy", "p", body="return 1")
    _plant_task(root, "bad", "q", body="raise ValueError('nope')")
    _plant_task(root, "happy2", "r", body="return 2")

    rows = run_tasks_concurrent([
        ("happy", "p", {}),
        ("bad", "q", {}),
        ("happy2", "r", {}),
    ])
    assert rows[0]["ok"] is True
    assert rows[1]["ok"] is False
    assert rows[1]["error_type"] == "ValueError"
    assert "nope" in rows[1]["error_msg"]
    assert rows[2]["ok"] is True


def test_empty_input_returns_empty(tmp_bs_home):
    from browser_skill.multitask import run_tasks_concurrent

    assert run_tasks_concurrent([]) == []


def test_kwargs_forwarded(tmp_bs_home):
    from browser_skill.memory.site_mem import site_skills_root
    from browser_skill.multitask import run_tasks_concurrent
    root = site_skills_root()
    (root / "withargs" / "tasks").mkdir(parents=True)
    (root / "withargs" / "memory.md").write_text("# w\n", encoding="utf-8")
    (root / "withargs" / "tasks" / "echo.py").write_text(
        '"""echo"""\n'
        'ARGS = {"value": {"type": "str", "required": True}}\n'
        'def selftest(): return True\n'
        'def run(args, ctx=None): return args["value"]\n',
        encoding="utf-8",
    )
    rows = run_tasks_concurrent([
        ("withargs", "echo", {"value": "alpha"}),
        ("withargs", "echo", {"value": "beta"}),
    ])
    assert rows[0]["value"] == "alpha"
    assert rows[1]["value"] == "beta"


def test_elapsed_sec_recorded(tmp_bs_home):
    from browser_skill.memory.site_mem import site_skills_root
    from browser_skill.multitask import run_tasks_concurrent
    root = site_skills_root()
    _plant_task(root, "sl", "z", body="import time; time.sleep(0.03); return 1")
    rows = run_tasks_concurrent([("sl", "z", {})])
    assert rows[0]["elapsed_sec"] >= 0.03
