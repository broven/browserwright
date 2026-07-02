"""Non-browser-driving primitive surface.

The legacy CDP browser-driving primitives (``open`` / ``goto_url`` /
``click_at_xy`` / ``js`` / ``cdp`` / ``capture_screenshot`` / ``snapshot`` /
the whole page/tab interaction stack) are DELETED — the agent drives the
browser with real Playwright via the injected ``page`` / ``context`` and
observes with ``snapshot()`` (see ``repl/_namespace.build_globals``). The
internal tab lifecycle that binding glue still needs lives in
``browserwright.session_runtime``.

What remains here is exactly the set re-exported by ``browserwright.api``:
``http_get`` (no-browser escape hatch), the site/memory verbs, and the
site-skill discovery/task layer. Keep it boring — no decorators, no
metaprogramming — so the agent gets stable, greppable names.
"""
from .discovery_api import (  # noqa: F401
    list_site_skills,
    load_site_skill,
    run_task,
)
from .http import http_get  # noqa: F401
from .site import (  # noqa: F401
    bootstrap_site,
    memory_read,
    remember,
    remember_global,
    remember_preference,
)

__all__ = [
    "list_site_skills", "load_site_skill", "run_task",
    "http_get",
    "bootstrap_site", "memory_read", "remember", "remember_global",
    "remember_preference",
]
