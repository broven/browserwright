"""S3 gate: the ``reload()`` navigation primitive is promoted into core and
actually reloads the page (resetting in-page state).

Two layers, mirroring test_perception.py:

1. **Surface contract (offline, always runs).** ``reload`` is in
   ``browser_skill.EXPORTS``, importable from the top-level namespace, and
   lands in the assembled REPL globals. That is what makes it a free name
   inside an agent heredoc — the whole point of "in core".

2. **Behaviour (live, needs a Chrome/Chromium binary).** Reuse the
   ``live_session`` fixture from test_perception (a real headless Chromium
   driven through the skill's own CDP transport). Set ``window.__s3 = 1`` via
   ``js()``, call ``reload()``, then assert the variable is gone — a reload
   tears down the JS context, so a stale ``js()`` returning the same value
   would mean the page never reloaded. Skips cleanly without Chromium.
"""
from __future__ import annotations

import pytest

# Pull in the live-Chromium fixture defined in test_perception.py. pytest
# resolves fixtures by name across test modules in the same directory.
from test_perception import live_session  # noqa: F401


# ---------------------------------------------------------------------------
# Surface contract — offline, deterministic, the durable regression core.
# ---------------------------------------------------------------------------


def test_reload_in_exports():
    import browser_skill

    assert "reload" in browser_skill.EXPORTS


def test_reload_importable_from_namespace():
    from browser_skill import reload  # noqa: F401

    assert callable(reload)


def test_reload_in_repl_globals(tmp_bs_home):
    from browser_skill.repl._namespace import build_globals

    g = build_globals()
    assert "reload" in g and callable(g["reload"])


def test_reload_accepts_hard_kwarg():
    """``hard=`` must be a real keyword-only parameter, not silently ignored."""
    import inspect as _inspect

    from browser_skill import reload

    sig = _inspect.signature(reload)
    assert "hard" in sig.parameters
    assert sig.parameters["hard"].kind == _inspect.Parameter.KEYWORD_ONLY


# ---------------------------------------------------------------------------
# Behaviour — live headless Chromium driven through the skill's CDP transport.
# ---------------------------------------------------------------------------


def test_reload_resets_in_page_state(live_session):  # noqa: F811
    from browser_skill import js, reload

    # Plant a sentinel on the live JS context.
    js("window.__s3 = 1; return window.__s3")
    assert js("return typeof window.__s3") == "number"

    # A real reload tears down and rebuilds the JS context, dropping __s3.
    reload()

    assert js("return typeof window.__s3") == "undefined"
