"""browserwright — Layer 2 of the browser stack.

Public surface: ``browserwright.cli:main`` is the CLI entry point.

For programmatic use inside REPL/task scripts, import names from the top-level
``browserwright`` namespace (everything from ``browserwright.api`` is re-exported
here)::

    from browserwright import http_get, remember, run_task

Browser driving itself is done with real Playwright in heredocs via the
injected ``page`` / ``context`` (and ``snapshot()``) — see
``repl/_namespace.build_globals``. Those are NOT importable from this module;
they are bound per-heredoc to the session's current tab.
"""
from .version import __version__  # noqa: F401

# Re-export the primitive namespace assembled in api.py so user scripts can
# `from browserwright import *`. The REPL/inline/task entry points use the same
# helper to populate their exec globals.
from .api import EXPORTS  # noqa: F401
from .api import *  # noqa: F401,F403
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
)
