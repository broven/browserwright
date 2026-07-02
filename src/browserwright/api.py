"""Canonical primitive surface for ``from browserwright import *``.

The inline / repl / task entry points all assemble their exec globals from
this module. Keeping the list in one place means an agent who imports
``browserwright`` directly from a saved task gets the same names the REPL
gave them.

The legacy CDP browser-driving primitives (``open``/``goto_url``/
``click_at_xy``/``js``/``cdp``/``capture_screenshot``/``snapshot``/… — the
whole page/tab interaction surface) are DELETED. The agent drives the browser
with **real Playwright** via the injected ``page`` / ``context`` (bound to the
session's current tab, reused across heredocs) and observes with
``snapshot()`` (a first-party AI aria snapshot whose ``[ref=eN]`` refs feed
``page.locator("aria-ref=eN")``). Those three names are injected per-heredoc
by ``repl/_namespace.build_globals``, NOT exported here. The internal tab
lifecycle the Playwright binding glue relies on lives in
``browserwright.session_runtime``.

What remains in EXPORTS is the set of NON-browser-driving helpers that do not
overlap Playwright: ``http_get`` (no-browser escape hatch), the memory verbs,
and the site-skill / task layer.
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
    # task (site-skills run on the Playwright surface — see
    # task_runner.run_task, which injects page/context into the task module)
    "list_site_skills", "load_site_skill", "run_task",
    # errors
    "BrowserwrightError", "PageLoadFailed", "ElementNotFound", "AuthWall",
    "Captcha", "NetworkError", "DaemonUnavailable", "CDPError",
    "NeedsUserConfirm",
]

__all__ = EXPORTS
