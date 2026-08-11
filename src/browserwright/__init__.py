"""browserwright — Layer 2 of the browser stack.

Public surface: ``browserwright.cli:main`` is the CLI entry point.

For programmatic use inside REPL/task scripts, import names from the top-level
``browserwright`` namespace::

    from browserwright import http_get, remember, run_task

``EXPORTS`` below is the canonical primitive surface. The inline / repl / task
entry points all assemble their exec globals from it
(``repl/_namespace.build_globals``), and ``skill_doc`` renders the generated
skill documentation from it — so the list an agent reads always matches the
list the binary actually binds. Keeping it in one place means an agent who
imports ``browserwright`` directly from a saved task gets the same names the
REPL gave them.

The legacy CDP browser-driving primitives (``open``/``goto_url``/
``click_at_xy``/``js``/``cdp``/``capture_screenshot``/``snapshot``/… — the
whole page/tab interaction surface) are DELETED. The agent drives the browser
with **real Playwright** via the injected ``page`` / ``context`` (bound to the
session's current tab, reused across heredocs) and observes with ``snapshot()``
(a first-party AI aria snapshot whose ``[ref=eN]`` refs feed
``page.locator("aria-ref=eN")``). Those three names are injected per-heredoc by
``repl/_namespace.build_globals``, NOT exported here; they are bound per call
to the session's current tab. The internal tab lifecycle the Playwright binding
glue relies on lives in ``browserwright.session_runtime``.

What remains in EXPORTS is the set of helpers that do not overlap Playwright:
``http_get`` (no-browser escape hatch), the memory verbs, the site-skill /
task layer, and the session tab-management pair ``tabs()`` / ``switch_tab()``
(Playwright cannot express "the session's current tab", so these are
first-class primitives, documented in the generated skill).
"""
from .tab_surface import TabMatchError, switch_tab, tabs  # noqa: F401
from .version import __version__  # noqa: F401

from .errors import (  # noqa: F401
    AuthWall,
    BrowserwrightError,
    Captcha,
    CDPError,
    DaemonUnavailable,
    ElementNotFound,
    NeedsUserConfirm,
    NetworkError,
    PageLoadFailed,
    UnsupportedContentType,
)
from .primitives.discovery_api import (  # noqa: F401
    list_site_skills,
    load_site_skill,
    run_task,
)
from .primitives.http import http_get  # noqa: F401
from .primitives.site import (  # noqa: F401
    bootstrap_site,
    memory_read,
    remember,
    remember_global,
    remember_preference,
)

EXPORTS = [
    # http (escape hatch — no browser; does not overlap Playwright)
    "http_get",
    # session tab management (Playwright cannot express the session's
    # current tab; see tab_surface.py)
    "tabs", "switch_tab",
    # memory + site
    "bootstrap_site", "remember", "remember_global", "remember_preference",
    "memory_read",
    # task (site-skills run on the Playwright surface — see
    # task_runner.run_task, which injects page/context into the task module)
    "list_site_skills", "load_site_skill", "run_task",
    # errors
    "BrowserwrightError", "PageLoadFailed", "ElementNotFound", "AuthWall",
    "Captcha", "NetworkError", "DaemonUnavailable", "CDPError",
    "NeedsUserConfirm", "UnsupportedContentType", "TabMatchError",
]

__all__ = EXPORTS
