"""Loader + runner for ``site-skills/<site>/tasks/<name>.py`` modules.

Phase C PR3: a site-skill task drives the browser with the SAME surface inline
execution gets — real Playwright ``page`` / ``context`` bound to the session's
current tab, plus ``snapshot()``. ``run()`` reads those as free globals
(``page.goto(...)``), so the runner injects them into the loaded module's
namespace before calling ``run``.

The resident executor can pass its already-live :class:`BrowserSurface`; that
path never constructs or closes a second Playwright connection. Standalone
callers retain the original lazy-handle behavior: a task that never touches the
browser opens no connection, and the runner tears down the handle after use.
"""
from __future__ import annotations

import importlib.util
from dataclasses import dataclass
from typing import Any, Callable

from .discovery import find_task_path


@dataclass(frozen=True, slots=True)
class BrowserSurface:
    """The executor-owned browser objects a task may borrow for one run.

    Ownership stays with the caller: ``run_task`` neither closes these objects
    nor their transport. A three-tuple of ``(page, context, snapshot)`` is also
    accepted at the private ``_browser_surface`` boundary for lightweight
    internal callers.
    """

    page: Any
    context: Any
    snapshot: Callable[..., str]


@dataclass(slots=True)
class _BoundPageHolder:
    """Duck-typed handle used to bind ``make_snapshot`` to one live page."""

    page: Any


def _coerce_browser_surface(
    surface: BrowserSurface | tuple[Any, Any, Callable[..., str]],
) -> BrowserSurface:
    if isinstance(surface, BrowserSurface):
        return surface
    if isinstance(surface, tuple) and len(surface) == 3:
        return BrowserSurface(
            page=surface[0],
            context=surface[1],
            snapshot=surface[2],
        )
    raise TypeError(
        "_browser_surface must be BrowserSurface or "
        "(page, context, snapshot)"
    )


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


def run_task(
    site: str,
    name: str,
    *,
    isolated: bool = False,
    **kwargs,
) -> Any:
    """Load + execute ``run(args, ctx=...)``.

      - ``OUTPUT_SCHEMA`` (if defined on the module) validates ``run()``
        return shape; mismatch raises ``BrowserwrightError`` with details.
      - ``isolated=True`` runs the task in its own ``Session`` pushed onto the
        ``ContextVar`` and binds it to a new page.
        Default ``False`` keeps the single-task / REPL behavior — same Session
        is reused, same target tracking, no extra ws roundtrips.
    """
    return _run_task(
        site,
        name,
        isolated=isolated,
        browser_surface=None,
        kwargs=kwargs,
    )


def _run_task_on_surface(
    site: str,
    name: str,
    *,
    browser_surface: (
        BrowserSurface | tuple[Any, Any, Callable[..., str]]
    ),
    isolated: bool = False,
    **kwargs,
) -> Any:
    """Executor-only task entrypoint borrowing its one Playwright surface.

    Keeping this separate from public :func:`run_task` avoids reserving a
    user-visible task argument such as ``_browser_surface``.
    """
    return _run_task(
        site,
        name,
        isolated=isolated,
        browser_surface=_coerce_browser_surface(browser_surface),
        kwargs=kwargs,
    )


def _run_task(
    site: str,
    name: str,
    *,
    isolated: bool,
    browser_surface: BrowserSurface | None,
    kwargs: dict[str, Any],
) -> Any:
    from .output_schema import validate as _validate_output
    from .repl.snapshot import make_snapshot
    from .session import _borrowed_session, isolated_session, with_session

    mod, path = _load(site, name)
    args = _validate_args(kwargs, getattr(mod, "ARGS", {}))

    def _run_inner(browser_surface: BrowserSurface | None = None) -> Any:
        from .memory import site_memory
        try:
            mem = site_memory(site).read()
        except Exception:
            mem = {"frontmatter": {}, "body": ""}

        handle = None
        if browser_surface is None:
            from .repl.playwright_handle import (
                PlaywrightHandle,
                _LazyHandleProxy,
            )

            # Standalone path: give the task the Playwright surface lazily (no
            # connection until first `page`/`context`/`snapshot` use).
            handle = PlaywrightHandle()
            page = _LazyHandleProxy(handle, "page")
            context = _LazyHandleProxy(handle, "context")
            snapshot = make_snapshot(handle)
        else:
            # Borrow the live executor surface verbatim. In particular, retain
            # its snapshot callable so refs stay scoped to the executor's page.
            page = browser_surface.page
            context = browser_surface.context
            snapshot = browser_surface.snapshot

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
            if handle is not None:
                # Tear down only a handle this call owns (no-op if never used).
                try:
                    handle.close()
                except Exception:  # noqa: BLE001
                    pass
        schema = getattr(mod, "OUTPUT_SCHEMA", None)
        if schema:
            _validate_output(result, schema, site=site, task=name)
        return result

    if browser_surface is not None:
        if not isolated:
            return _run_inner(browser_surface)
        # Executor-owned isolation stays inside the executor's one Playwright
        # context/transport. Give session-based helpers (for example
        # remember(None, ...)) a matching isolated Session as well, so they
        # resolve the new page rather than the executor's parent page.
        from .session import current_session
        from .session_runtime import session_tabs

        context = browser_surface.context
        existing_target_ids = {
            target.get("targetId")
            for target in session_tabs(current_session())
            if isinstance(target.get("targetId"), str)
        }
        page = context.new_page()
        isolated_surface = BrowserSurface(
            page=page,
            context=context,
            snapshot=make_snapshot(_BoundPageHolder(page)),
        )
        sess = _borrowed_session()
        try:
            _bind_session_to_page(
                sess,
                page,
                exclude_target_ids=existing_target_ids,
            )
            with with_session(sess):
                return _run_inner(isolated_surface)
        finally:
            # This closes only the isolated agent-path transport. The executor
            # retains ownership of Playwright and neither page is closed.
            sess.close()
    if not isolated:
        return _run_inner()
    sess = isolated_session()
    try:
        with with_session(sess):
            import uuid
            from .session_runtime import open_session_tab
            prebind_url = (
                "data:text/html;charset=utf-8,"
                f"<title>browserwright-isolated-{uuid.uuid4().hex}</title>"
            )
            open_session_tab(sess, prebind_url)
            return _run_inner()
    finally:
        # The isolated Session owns the CDP it lazily opened during this run;
        # closing it now releases per-task resources without affecting the
        # default singleton.
        sess.close()


def _bind_session_to_page(
    sess: Any,
    page: Any,
    *,
    exclude_target_ids: set[str] | None = None,
    timeout: float = 2.0,
) -> None:
    """Bind an isolated agent Session to an exact executor-created Page.

    Playwright's Python Page does not expose Chromium's target id. Mark the Page
    in its main world, inspect the session's agent-path targets for that marker,
    and set only the isolated Session's in-memory current target. The shared
    ledger stays pointed at the executor's primary page.
    """
    import json
    import time
    from uuid import uuid4

    from .errors import PageBindTimeout
    from .session_runtime import session_tabs

    key = f"__browserwright_isolated_{uuid4().hex}"
    value = uuid4().hex
    excluded = exclude_target_ids or set()
    deadline = time.monotonic() + max(0.0, timeout)
    try:
        while True:
            try:
                # Re-install each pass: a navigation swaps the page's Window
                # and drops the prior marker.
                page.evaluate(
                    "([key, value]) => { Object.defineProperty("
                    "globalThis, key, {value, configurable: true}); "
                    "return true; }",
                    [key, value],
                )
            except Exception:
                pass
            for target in session_tabs(sess):
                target_id = target.get("targetId")
                if not isinstance(target_id, str) or not target_id:
                    continue
                if target_id in excluded:
                    continue
                try:
                    session_id = sess.cdp.attach(target_id)
                    result = sess.cdp.send(
                        "Runtime.evaluate",
                        session=session_id,
                        expression=(
                            f"globalThis[{json.dumps(key)}] === "
                            f"{json.dumps(value)}"
                        ),
                        returnByValue=True,
                    )
                    matched = (
                        result.get("result", {}).get("value") is True
                        and not result.get("exceptionDetails")
                    )
                except Exception:
                    matched = False
                if matched:
                    sess.current_target_id = target_id
                    return
            if time.monotonic() >= deadline:
                raise PageBindTimeout(
                    target_id="<isolated task page>",
                    timeout=timeout,
                )
            time.sleep(0.05)
    finally:
        try:
            page.evaluate(
                "key => delete globalThis[key]",
                key,
            )
        except Exception:
            pass
