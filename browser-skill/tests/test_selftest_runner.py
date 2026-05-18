"""Selftest cron primitive tests (v0.3)."""
from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest


def _reset_modules():
    for k in list(sys.modules):
        if k.startswith("browser_skill"):
            del sys.modules[k]


@pytest.fixture(autouse=True)
def _skip_bundled(monkeypatch, tmp_path):
    """Bundled site-skills (google/github/HN/wikipedia/producthunt) need a
    live Chrome — selftest probes them every test run otherwise, ~40s wasted.
    Point ``_bundled_root`` at an empty dir for the test scope."""
    import sys
    for k in list(sys.modules):
        if k.startswith("browser_skill"):
            del sys.modules[k]
    empty = tmp_path / "empty-bundle"
    empty.mkdir()
    from browser_skill import discovery
    monkeypatch.setattr(discovery, "_bundled_root", lambda: empty)
    yield


def _plant_task(site_dir: Path, name: str, *, selftest_body: str = "return True"):
    (site_dir / "tasks").mkdir(parents=True, exist_ok=True)
    (site_dir / "memory.md").write_text(f"# {site_dir.name}\n", encoding="utf-8")
    (site_dir / "tasks" / f"{name}.py").write_text(
        f'"""{name}"""\n'
        f'ARGS = {{}}\n'
        f'def selftest():\n'
        f'    {selftest_body}\n'
        f'def run(args, ctx=None):\n'
        f'    return "ok"\n',
        encoding="utf-8",
    )


def test_runs_all_discovered_tasks(tmp_bs_home, monkeypatch):
    """Every discovered task is invoked; verdict + cache reflect outcomes."""
    # Note: don't call _reset_modules() here — the autouse fixture has
    # already cleared and re-imported with the patched _bundled_root.
    # Re-clearing would lose the monkeypatch on the discovery module.
    from browser_skill.memory.site_mem import site_skills_root
    root = site_skills_root()
    _plant_task(root / "good-site", "happy", selftest_body="return True")
    _plant_task(root / "bad-site", "drift", selftest_body="assert False, 'drifted'")
    _plant_task(root / "errsite", "boom", selftest_body="raise RuntimeError('boom')")
    _plant_task(root / "skipsite", "manual")
    # Make `manual` selftest return False (counts as fail, not skip — skip
    # is reserved for "no selftest defined").
    (root / "skipsite" / "tasks" / "manual.py").write_text(
        '"""manual"""\nARGS = {}\ndef run(args, ctx=None): return None\n',
        encoding="utf-8",
    )

    from browser_skill.selftest_runner import run_all
    summary = run_all()
    by_site = {(r["site"], r["name"]): r for r in summary["results"]}
    assert by_site[("good-site", "happy")]["verdict"] == "ok"
    assert by_site[("bad-site", "drift")]["verdict"] == "fail"
    assert by_site[("errsite", "boom")]["verdict"] == "error"
    assert by_site[("skipsite", "manual")]["verdict"] == "skip"
    assert summary["totals"]["ok"] >= 1
    assert summary["totals"]["fail"] >= 1
    assert summary["totals"]["error"] >= 1
    assert summary["totals"]["skip"] >= 1


def test_site_filter(tmp_bs_home, monkeypatch):
    # Note: don't call _reset_modules() here — the autouse fixture has
    # already cleared and re-imported with the patched _bundled_root.
    # Re-clearing would lose the monkeypatch on the discovery module.
    from browser_skill.memory.site_mem import site_skills_root
    root = site_skills_root()
    _plant_task(root / "x", "t")
    _plant_task(root / "y", "t")
    from browser_skill.selftest_runner import run_all
    summary = run_all(site="x")
    assert all(r["site"] == "x" for r in summary["results"])


def test_passes_update_cache(tmp_bs_home, monkeypatch):
    # Note: don't call _reset_modules() here — the autouse fixture has
    # already cleared and re-imported with the patched _bundled_root.
    # Re-clearing would lose the monkeypatch on the discovery module.
    from browser_skill.memory.site_mem import site_skills_root
    _plant_task(site_skills_root() / "cached", "ping")
    from browser_skill.selftest_runner import run_all
    from browser_skill import selftest_cache
    run_all(site="cached")
    task_path = site_skills_root() / "cached" / "tasks" / "ping.py"
    assert selftest_cache.is_fresh("cached", "ping", task_path) is True


def test_cli_selftest_run_returns_code(tmp_bs_home, monkeypatch):
    """Run via CLI helper to confirm exit-code semantics:
    0 on all pass, 1 on any fail/error, 2 on empty result."""
    # Note: don't call _reset_modules() here — the autouse fixture has
    # already cleared and re-imported with the patched _bundled_root.
    # Re-clearing would lose the monkeypatch on the discovery module.
    from browser_skill.memory.site_mem import site_skills_root
    _plant_task(site_skills_root() / "happy", "p")
    from browser_skill.cli import _cmd_selftest
    rc = _cmd_selftest(["run", "--site=happy"])
    assert rc == 0

    _plant_task(site_skills_root() / "drift", "x", selftest_body="assert False")
    rc = _cmd_selftest(["run", "--site=drift"])
    assert rc == 1


def test_cli_selftest_run_no_results(tmp_bs_home, monkeypatch):
    _reset_modules()
    from browser_skill.cli import _cmd_selftest
    rc = _cmd_selftest(["run", "--site=nonexistent"])
    assert rc == 2
