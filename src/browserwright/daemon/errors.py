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


class ChromeBinaryNotFound(Unavailable):
    """launch-chrome could not locate a Chrome binary. Exit code 6."""
