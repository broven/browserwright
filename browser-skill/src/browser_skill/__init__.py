"""browser-skill — Layer 2 of the browser stack.

Public surface: ``browser_skill.cli:main`` is the CLI entry point.

For programmatic use inside REPL/task scripts, import names from the top-level
``browser_skill`` namespace (everything from ``browser_skill.api`` is re-exported
here)::

    from browser_skill import goto_url, capture_screenshot, remember
"""
__version__ = "0.5.1"

# Re-export the primitive namespace assembled in api.py so user scripts can
# `from browser_skill import *`. The REPL/inline/task entry points use the same
# helper to populate their exec globals.
from .api import EXPORTS  # noqa: F401
from .api import *  # noqa: F401,F403
from .errors import (  # noqa: F401
    AuthWall,
    BrowserSkillError,
    Captcha,
    CDPError,
    DaemonUnavailable,
    ElementNotFound,
    NeedsUserConfirm,
    NetworkError,
    PageLoadFailed,
    SiteDrift,
)
