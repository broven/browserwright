"""Assemble the exec globals shared by inline / repl-server / task entry points.

Every entry point hot-imports ``browser_skill`` so the agent always sees the
same names (``goto_url``, ``remember``, etc.). We also pull ``json``, ``re``,
``time``, and a handful of builtins that agents reach for constantly — saves
a ``import`` line per heredoc.
"""
from __future__ import annotations

import json
import re
import sys
import time
from typing import Any

import browser_skill


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
    return g
