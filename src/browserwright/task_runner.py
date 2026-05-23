"""Loader + runner for ``site-skills/<site>/tasks/<name>.py`` modules."""
from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any

from .discovery import find_task_path
from .errors import SiteDrift


class _Ctx:
    """Minimal context object passed to ``run(args, ctx=...)``."""

    def __init__(self, *, memory: dict):
        self.memory = memory


def _validate_args(args: dict, schema: dict) -> dict:
    """Light coercion: fill defaults, complain about missing required."""
    out = dict(args)
    for key, meta in (schema or {}).items():
        if key not in out:
            if meta.get("required"):
                raise ValueError(f"missing required arg: {key}")
            if "default" in meta:
                out[key] = meta["default"]
    return out


def _load(site: str, name: str):
    path = find_task_path(site, name)
    spec = importlib.util.spec_from_file_location(f"site_skills_{site}_{name}", path)
    if not spec or not spec.loader:
        raise FileNotFoundError(f"could not load task module: {path}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod, path


def run_task(site: str, name: str, *, isolated: bool = False, **kwargs) -> Any:
    """Load + (cached) selftest + execute ``run(args, ctx=...)``.

    v0.2 added:
      - selftest result is cached for 24h keyed on the task file hash; pass
        ``BS_SELFTEST_NOCACHE=1`` to force a re-run.
      - ``OUTPUT_SCHEMA`` (if defined on the module) validates ``run()``
        return shape; mismatch raises ``BrowserSkillError`` with details.

    v0.3 added:
      - ``isolated=True`` runs the task in its own ``Session`` pushed onto the
        ``ContextVar`` for the duration of ``run()``. Other concurrently-
        executing tasks see *their* sessions via ``current_session()``, so
        ``new_tab`` / ``current_target_id`` don't collide.
        Default ``False`` keeps the single-task / REPL behavior — same Session
        is reused, same target tracking, no extra ws roundtrips.
    """
    from . import selftest_cache
    from .output_schema import validate as _validate_output
    from .session import Session, with_session

    mod, path = _load(site, name)
    args = _validate_args(kwargs, getattr(mod, "ARGS", {}))

    def _run_inner() -> Any:
        selftest = getattr(mod, "selftest", None)
        if callable(selftest) and not selftest_cache.is_fresh(site, name, path):
            try:
                ok = selftest()
            except AssertionError as e:
                selftest_cache.remember_fail(site, name, path, str(e))
                raise SiteDrift(site=site, task=name, failed_check=str(e)) from e
            if ok is False:
                selftest_cache.remember_fail(site, name, path, "selftest returned False")
                raise SiteDrift(site=site, task=name,
                                failed_check="selftest returned False")
            selftest_cache.remember_pass(site, name, path)
        from .memory import site_memory
        try:
            mem = site_memory(site).read()
        except Exception:
            mem = {"frontmatter": {}, "body": ""}
        ctx = _Ctx(memory=mem.get("frontmatter", {}))
        run = getattr(mod, "run", None)
        if not callable(run):
            raise ValueError(f"task module has no run(): {path}")
        result = run(args, ctx=ctx)
        schema = getattr(mod, "OUTPUT_SCHEMA", None)
        if schema:
            _validate_output(result, schema, site=site, task=name)
        return result

    if not isolated:
        return _run_inner()
    sess = Session()
    try:
        with with_session(sess):
            return _run_inner()
    finally:
        # The isolated Session owns the CDP it lazily opened during this run;
        # closing it now releases per-task resources without affecting the
        # default singleton.
        sess.close()
