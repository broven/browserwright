"""Daemon-internal exception hierarchy.

These map to Mode A exit codes (§5.1):
- UserError    -> 1
- Unavailable  -> 2
- ChromeBinaryNotFound (subclass of Unavailable) -> 6 from launch-chrome (§5.5)
- everything else (uncaught) -> 3
"""
from __future__ import annotations


class DaemonError(Exception):
    """Base class. Subclasses choose exit-code semantics."""


class UserError(DaemonError):
    """Bad CLI input — unknown backend name, invalid flag combination, malformed BD_NAME."""


class Unavailable(DaemonError):
    """No backend could resolve a ws URL.

    Carries an optional dict mapping backend-name -> per-backend reason so the CLI
    can show all candidates that were tried. Single-backend failure (when --backend
    was explicit) collapses to one entry.
    """

    def __init__(self, message: str, attempts: dict[str, str] | None = None):
        super().__init__(message)
        self.attempts = attempts or {}


class DaemonShuttingDown(Unavailable):
    """This daemon is on its way out, so it refuses to prepare new work.

    Deliberately its own type with its own words (ADR-0013 rule 3). While a
    daemon is replaced, closing the relay makes every readiness wait look like
    "no extension is connected" — a lie an agent acts on at chrome://extensions
    while the extension was never involved (2026-10-19: three calls died that
    way with the browser connected and healthy throughout).

    ``MARKER`` is the half that survives the JSON-RPC error envelope as text;
    `_executor/client.py` keys its (safe, pre-request) retry on it, so the
    phrase and the client's classifier must stay in step.
    """

    MARKER = "the daemon is shutting down"

    def __init__(self, session_id: str):
        super().__init__(
            f"{self.MARKER} (it is being replaced or stopped), so it will not "
            f"prepare an executor for session {session_id!r}; the browser and "
            f"the extension are not involved. Retry the command once it "
            f"answers again.",
        )


class ChromeBinaryNotFound(Unavailable):
    """launch-chrome could not locate a Chrome binary. Exit code 6."""
