"""Selftest cron primitive (v0.3).

Provides the building block Layer 3 / OS cron can invoke to refresh the
``selftest_cache.json`` health record across all bundled / discovered tasks::

    browser-skill selftest run                  # every task across all sites
    browser-skill selftest run --site github.com
    browser-skill selftest run --json           # machine-readable summary

The runner loads each task module, invokes its ``selftest()``, and records
the verdict in the cache. Failures are logged but don't abort the run — the
caller wants the full health picture.

Layer 3 (cron) just shells out to this and trusts the cache afterwards.
"""
from __future__ import annotations

import importlib.util
import time
from pathlib import Path
from typing import Optional

from . import selftest_cache
from .discovery import _iter_site_dirs


def _load_module(task_path: Path):
    spec = importlib.util.spec_from_file_location(
        f"selftest_probe_{task_path.parent.parent.name}_{task_path.stem}", task_path)
    if not spec or not spec.loader:
        return None
    mod = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(mod)
    except Exception:
        return None
    return mod


def run_all(*, site: Optional[str] = None, isolated: bool = False) -> dict:
    """Run every cached task's selftest. Returns a structured summary::

        {
          "ts": 1234567890.0,
          "duration_sec": 12.5,
          "results": [
            {"site": "...", "name": "...", "verdict": "ok"|"fail"|"skip"|"error",
             "reason": "..."},
            ...
          ],
          "totals": {"ok": N, "fail": N, "skip": N, "error": N},
        }

    ``site=None`` runs every discovered site. ``site="..."`` filters to one.
    ``isolated=True`` runs each task's selftest in its own ``Session`` — useful
    once Layer 3 wants to parallelise across a multi-client daemon.
    """
    from .session import Session, with_session

    started = time.time()
    results: list[dict] = []
    totals = {"ok": 0, "fail": 0, "skip": 0, "error": 0}

    for site_dir in _iter_site_dirs():
        if site is not None and site_dir.name != site:
            continue
        tasks_dir = site_dir / "tasks"
        if not tasks_dir.is_dir():
            continue
        for task_path in sorted(tasks_dir.glob("*.py")):
            entry = _run_one(site_dir.name, task_path, isolated=isolated)
            results.append(entry)
            totals[entry["verdict"]] = totals.get(entry["verdict"], 0) + 1
    return {
        "ts": started,
        "duration_sec": round(time.time() - started, 3),
        "results": results,
        "totals": totals,
    }


def _run_one(site_name: str, task_path: Path, *, isolated: bool) -> dict:
    name = task_path.stem
    mod = _load_module(task_path)
    if mod is None:
        return {"site": site_name, "name": name, "verdict": "error",
                "reason": "failed to import module"}
    selftest = getattr(mod, "selftest", None)
    if not callable(selftest):
        return {"site": site_name, "name": name, "verdict": "skip",
                "reason": "no selftest() defined"}

    def _invoke():
        try:
            ok = selftest()
        except AssertionError as e:
            selftest_cache.remember_fail(site_name, name, task_path, str(e))
            return {"site": site_name, "name": name, "verdict": "fail",
                    "reason": str(e)}
        except Exception as e:  # noqa: BLE001 — agent-friendly catch-all
            selftest_cache.remember_fail(site_name, name, task_path, repr(e))
            return {"site": site_name, "name": name, "verdict": "error",
                    "reason": repr(e)}
        if ok is False:
            selftest_cache.remember_fail(site_name, name, task_path,
                                         "selftest returned False")
            return {"site": site_name, "name": name, "verdict": "fail",
                    "reason": "returned False"}
        selftest_cache.remember_pass(site_name, name, task_path)
        return {"site": site_name, "name": name, "verdict": "ok",
                "reason": "passed"}

    if not isolated:
        return _invoke()
    from .session import Session, with_session
    sess = Session()
    try:
        with with_session(sess):
            return _invoke()
    finally:
        sess.close()
