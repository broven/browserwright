"""Assemble the exec globals shared by inline / repl-server / task entry points.

Every entry point hot-imports ``browser_skill`` so the agent always sees the
same names (``goto_url``, ``remember``, etc.). We also pull ``json``, ``re``,
``time``, and a handful of builtins that agents reach for constantly — saves
a ``import`` line per heredoc.

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

import browser_skill


def _load_agent_helpers(g: dict[str, Any]) -> None:
    """Inject agent-authored helpers from ``$BS_HOME/agent_helpers.py`` into
    ``g``. No-op when the file is absent. Names already in ``g`` (the core
    primitive surface + stdlib helpers) are protected: a shadowing definition
    is refused with a stderr warning, never silently applied. Underscore-
    prefixed names stay private. A broken file warns but never breaks the
    namespace — the core surface must always come up.
    """
    # Imported lazily to avoid a module-import cycle (memory -> primitives).
    from browser_skill.memory.global_mem import home_dir

    path = home_dir() / "agent_helpers.py"
    if not path.exists():
        return

    protected = set(g)  # core EXPORTS + json/re/time/sys already placed
    try:
        spec = importlib.util.spec_from_file_location(
            "browser_skill_agent_helpers", path)
        if spec is None or spec.loader is None:
            return
        module = importlib.util.module_from_spec(spec)
        # Pre-seed the helper module's globals with the core surface so a
        # helper can call ``goto_url`` / ``js`` / ``upload_file`` etc. with no
        # import — a function resolves free names against its own module dict,
        # which is this one. (browser-harness requires explicit imports here.)
        module.__dict__.update(
            {k: v for k, v in g.items() if not k.startswith("__")})
        spec.loader.exec_module(module)
    except Exception as exc:  # syntax error, import error, anything
        print(f"browser-skill: failed to load agent_helpers.py ({path}): "
              f"{type(exc).__name__}: {exc}", file=sys.stderr)
        return

    for name, value in vars(module).items():
        if name.startswith("_"):
            continue
        if name in protected:
            # Same object (e.g. the helper did `import json`) → harmless skip.
            if g.get(name) is not value:
                print(f"browser-skill: agent helper {name!r} shadows a core "
                      f"primitive — ignored, keeping core (rename it)",
                      file=sys.stderr)
            continue
        g[name] = value


def build_globals() -> dict[str, Any]:
    g: dict[str, Any] = {}
    # Every primitive + every error class.
    for name in browser_skill.EXPORTS:
        g[name] = getattr(browser_skill, name)
    # Commonly-needed stdlib.
    g["json"] = json
    g["re"] = re
    g["time"] = time
    g["sys"] = sys
    g["__name__"] = "__skill__"
    g["__builtins__"] = __builtins__
    # Agent-editable layer last, so helpers can call core primitives.
    _load_agent_helpers(g)
    return g
