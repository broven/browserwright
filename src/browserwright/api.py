"""Canonical primitive surface for ``from browserwright import *``.

The inline / repl / task entry points all assemble their exec globals from
this module. Keeping the list in one place means an agent who imports
``browserwright`` directly from a saved task gets the same names the REPL
gave them.

Phase C PR3 (terminal state): the legacy CDP browser-driving primitives
(``open``/``goto_url``/``click_at_xy``/``js``/``cdp``/``capture_screenshot``/
``snapshot``/… — the whole page/tab interaction surface) are GONE from the
agent surface. The agent now drives the browser with **real Playwright** via
the injected ``page`` / ``context`` (bound to the session's current tab,
reused across heredocs) and observes with ``snapshot()`` (a first-party AI
aria snapshot whose ``[ref=eN]`` refs feed ``page.locator("aria-ref=eN")``).
Those three names are injected per-heredoc by ``repl/_namespace.build_globals``,
NOT exported here.

What remains in EXPORTS is the set of NON-browser-driving helpers that do not
overlap Playwright: ``http_get`` (no-browser escape hatch), the memory verbs,
and the site-skill / task layer. The implementation modules under
``primitives/`` still define the old functions (``current_page``, ``list_tabs``,
the daemon-driving glue, …); they are kept as INTERNAL functions the Phase C
binding glue (``repl/playwright_handle.py``) and the memory/site helpers rely
on — they are simply no longer part of the agent-callable surface.
"""
from .errors import (
    AuthWall,
    BrowserwrightError,
    Captcha,
    CDPError,
    DaemonUnavailable,
    ElementNotFound,
    NeedsUserConfirm,
    NetworkError,
    PageLoadFailed,
)
from .multitask import run_tasks_concurrent
from .primitives import (
    bootstrap_site,
    http_get,
    list_site_skills,
    load_site_skill,
    memory_read,
    remember,
    remember_global,
    remember_preference,
    run_task,
)

EXPORTS = [
    # http (escape hatch — no browser; does not overlap Playwright)
    "http_get",
    # memory + site
    "bootstrap_site", "remember", "remember_global", "remember_preference",
    "memory_read",
    # task / fan-out (site-skills run on the Playwright surface — see
    # task_runner.run_task, which injects page/context into the task module)
    "list_site_skills", "load_site_skill", "run_task",
    "run_tasks_concurrent",
    # errors
    "BrowserwrightError", "PageLoadFailed", "ElementNotFound", "AuthWall",
    "Captcha", "NetworkError", "DaemonUnavailable", "CDPError",
    "NeedsUserConfirm",
]

__all__ = EXPORTS
