"""Assemble the exec globals shared by inline / repl-server / task entry points.

Every entry point hot-imports ``browserwright`` so the agent always sees the
same names (``http_get``, ``remember``, etc.). It also injects the per-heredoc
Playwright surface (``page`` / ``context`` / ``snapshot``). We also pull
``json``, ``re``, ``time``, and a handful of builtins that agents reach for
constantly — saves a ``import`` line per heredoc.

Finally we hot-load ``$BS_HOME/agent_helpers.py`` — the agent-editable
primitive layer (see SKILL.md "Extending the primitive surface"). It loads
*after* the core surface so helpers can call core primitives, and a conflict
guard refuses any helper that would shadow a core name.
"""
from __future__ import annotations

import importlib.util
import json
import re
import sys
import time
from typing import Any

import browserwright


def _load_agent_helpers(g: dict[str, Any]) -> None:
    """Inject agent-authored helpers from ``$BS_HOME/agent_helpers.py`` into
    ``g``. No-op when the file is absent. Names already in ``g`` (the core
    primitive surface + stdlib helpers) are protected: a shadowing definition
    is refused with a stderr warning, never silently applied. Underscore-
    prefixed names stay private. A broken file warns but never breaks the
    namespace — the core surface must always come up.
    """
    # Imported lazily to avoid a module-import cycle (memory -> primitives).
    from browserwright.memory.global_mem import home_dir

    path = home_dir() / "agent_helpers.py"
    if not path.exists():
        return

    protected = set(g)  # core EXPORTS + json/re/time/sys already placed
    try:
        spec = importlib.util.spec_from_file_location(
            "browserwright_agent_helpers", path)
        if spec is None or spec.loader is None:
            return
        module = importlib.util.module_from_spec(spec)
        # Pre-seed the helper module's globals with the core surface so a
        # helper can call ``http_get`` / ``remember`` / ``run_task`` etc. with
        # no import — a function resolves free names against its own module
        # dict, which is this one. (browser-harness requires explicit imports
        # here.)
        module.__dict__.update(
            {k: v for k, v in g.items() if not k.startswith("__")})
        spec.loader.exec_module(module)
    except Exception as exc:  # syntax error, import error, anything
        print(f"browserwright: failed to load agent_helpers.py ({path}): "
              f"{type(exc).__name__}: {exc}", file=sys.stderr)
        return

    for name, value in vars(module).items():
        if name.startswith("_"):
            continue
        if name in protected:
            # Same object (e.g. the helper did `import json`) → harmless skip.
            if g.get(name) is not value:
                print(f"browserwright: agent helper {name!r} shadows a core "
                      f"primitive — ignored, keeping core (rename it)",
                      file=sys.stderr)
            continue
        g[name] = value


def build_globals() -> dict[str, Any]:
    g: dict[str, Any] = {}
    # Every primitive + every error class.
    for name in browserwright.EXPORTS:
        g[name] = getattr(browserwright, name)
    # Commonly-needed stdlib.
    g["json"] = json
    g["re"] = re
    g["time"] = time
    g["sys"] = sys
    g["__name__"] = "__skill__"
    g["__builtins__"] = __builtins__
    # Phase C: a lazy Playwright `page` / `context` bound to the session's
    # current tab. Both are transparent proxies that DON'T connect until first
    # use, so a pure memory()/site-skill heredoc opens no browser connection.
    # The owning handle is stashed under a private key so the heredoc runner
    # can tear the connection down at heredoc end (see inline.py). It is NOT a
    # core EXPORT — only entry points that drive a heredoc inject it here.
    from .playwright_handle import PlaywrightHandle, _LazyHandleProxy
    handle = PlaywrightHandle()
    g["page"] = _LazyHandleProxy(handle, "page")
    g["context"] = _LazyHandleProxy(handle, "context")
    g["__bw_playwright_handle__"] = handle
    # Phase C: `snapshot()` is the Playwright first-party AI aria snapshot
    # bound to THIS heredoc's `page` (refs → `page.locator("aria-ref=eN")`).
    # The legacy coordinate `snapshot` was removed from EXPORTS in PR3, so this
    # is now the sole observation verb the agent gets — there is nothing left
    # to override.
    from .snapshot import make_snapshot
    g["snapshot"] = make_snapshot(handle)
    # Agent-editable layer last, so helpers can call core primitives.
    _load_agent_helpers(g)
    return g
