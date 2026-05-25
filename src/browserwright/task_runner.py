"""Loader + runner for ``site-skills/<site>/tasks/<name>.py`` modules.

Phase C PR3: a site-skill task drives the browser with the SAME surface inline
execution gets — real Playwright ``page`` / ``context`` bound to the session's
current tab, plus ``snapshot()``. ``run()`` reads those as free globals
(``page.goto(...)``), so the runner injects them into the loaded module's
namespace before calling ``run`` and tears the lazy connection down after.
Like inline execution, the connection is LAZY: a task that never touches the
browser opens no connection.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any

from .discovery import find_task_path


class _Ctx:
    """Minimal context object passed to ``run(args, ctx=...)``.

    Carries the per-run site memory plus the live browser handles so a task
    can use either ``ctx.page`` / ``ctx.context`` / ``ctx.snapshot`` or the
    free-global ``page`` / ``context`` / ``snapshot`` the runner injects.
    """

    def __init__(self, *, memory: dict, page: Any = None,
                 context: Any = None, snapshot: Any = None):
        self.memory = memory
        self.page = page
        self.context = context
        self.snapshot = snapshot


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
    """Load + execute ``run(args, ctx=...)``.

      - ``OUTPUT_SCHEMA`` (if defined on the module) validates ``run()``
        return shape; mismatch raises ``BrowserwrightError`` with details.
      - ``isolated=True`` runs the task in its own ``Session`` pushed onto the
        ``ContextVar`` for the duration of ``run()``. Other concurrently-
        executing tasks see *their* sessions via ``current_session()``, so
        ``new_tab`` / ``current_target_id`` don't collide.
        Default ``False`` keeps the single-task / REPL behavior — same Session
        is reused, same target tracking, no extra ws roundtrips.
    """
    from .output_schema import validate as _validate_output
    from .session import isolated_session, with_session

    mod, path = _load(site, name)
    args = _validate_args(kwargs, getattr(mod, "ARGS", {}))

    def _run_inner() -> Any:
        from .memory import site_memory
        from .repl.playwright_handle import PlaywrightHandle, _LazyHandleProxy
        from .repl.snapshot import make_snapshot
        try:
            mem = site_memory(site).read()
        except Exception:
            mem = {"frontmatter": {}, "body": ""}

        # Phase C: give the task the Playwright surface, lazily (no connection
        # until first `page`/`context`/`snapshot` use). The proxies bind to the
        # session's current tab — same discipline as the heredoc namespace.
        handle = PlaywrightHandle()
        page = _LazyHandleProxy(handle, "page")
        context = _LazyHandleProxy(handle, "context")
        snapshot = make_snapshot(handle)
        ctx = _Ctx(memory=mem.get("frontmatter", {}),
                   page=page, context=context, snapshot=snapshot)
        # Inject as free globals so `run()` can call `page.goto(...)` directly.
        mod.page = page
        mod.context = context
        mod.snapshot = snapshot

        run = getattr(mod, "run", None)
        if not callable(run):
            raise ValueError(f"task module has no run(): {path}")
        try:
            result = run(args, ctx=ctx)
        finally:
            # Tear down the lazy Playwright connection (no-op if never used).
            try:
                handle.close()
            except Exception:  # noqa: BLE001
                pass
        schema = getattr(mod, "OUTPUT_SCHEMA", None)
        if schema:
            _validate_output(result, schema, site=site, task=name)
        return result

    if not isolated:
        return _run_inner()
    sess = isolated_session()
    try:
        with with_session(sess):
            return _run_inner()
    finally:
        # The isolated Session owns the CDP it lazily opened during this run;
        # closing it now releases per-task resources without affecting the
        # default singleton.
        sess.close()
