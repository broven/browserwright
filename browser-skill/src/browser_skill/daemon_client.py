"""Mode A daemon client — subprocess + stdout parsing + single retry.

Talks to the external ``browser-daemon`` CLI:

* ``browser-daemon url [--backend X]``  → single-line WS URL on stdout
* ``browser-daemon active-tab --json``  → {"targetId","url","title","accuracy","since_seconds"}
* ``browser-daemon doctor --json``      → schema_version:1 doctor blob

Mock mode (env ``BS_DAEMON_URL_CMD`` or ``BS_CDP_WS``):
  - If ``BS_CDP_WS`` is set, ``resolve_ws_url()`` returns it directly. This lets
    you point Skill at an already-running CDP endpoint (browser-harness daemon,
    Browser Use cloud, raw Chrome --remote-debugging-port=N, etc.) before the
    real ``browser-daemon`` binary is available.

The single-retry policy:
  - First call → if ``DaemonUnavailable`` raised, drop cache and retry once.
  - Second failure → raise to caller. No exponential backoff (spec §D.7).
"""
from __future__ import annotations

import json
import os
import shlex
import subprocess
from typing import Optional

from .errors import DaemonUnavailable


def _split_cmd(cmd: str) -> list[str]:
    return shlex.split(cmd) if isinstance(cmd, str) else list(cmd)


class DaemonClient:
    """Mode A subprocess client.

    Stateless except for a cached ws URL. Methods are sync — they fork
    ``browser-daemon`` for each call. That's acceptable for v0.1 because
    the long-lived REPL daemon (see ``repl.server``) only resolves once at
    startup and then re-uses the CDP ws via the in-process CDP transport.
    """

    def __init__(
        self,
        url_cmd: Optional[str] = None,
        backend: Optional[str] = None,
        daemon_bin: str = "browser-daemon",
    ):
        # Precedence: env (CI/test) > explicit arg > config default.
        self._url_cmd = url_cmd or os.environ.get(
            "BS_DAEMON_URL_CMD", f"{daemon_bin} url"
        )
        self._daemon_bin = daemon_bin
        self._backend = backend or os.environ.get("BS_DAEMON_BACKEND")
        self._cached_url: Optional[str] = None

    # ---- WS URL ----------------------------------------------------------

    def resolve_ws_url(self) -> str:
        """Return a browser-level CDP ws URL.

        Order:
          1. ``BS_CDP_WS`` env (explicit override / mock).
          2. cached value from previous successful resolve.
          3. ``browser-daemon url [--backend X]`` subprocess.
        """
        env_url = os.environ.get("BS_CDP_WS") or os.environ.get("BU_CDP_WS")
        if env_url:
            return env_url
        if self._cached_url:
            return self._cached_url
        return self._spawn_url(retry=True)

    def invalidate(self) -> None:
        self._cached_url = None

    def _spawn_url(self, retry: bool) -> str:
        cmd = _split_cmd(self._url_cmd)
        if self._backend:
            cmd += ["--backend", self._backend]
        try:
            proc = subprocess.run(
                cmd, capture_output=True, text=True, timeout=15
            )
        except FileNotFoundError as e:
            if retry:
                # No second chance if the binary is missing.
                raise DaemonUnavailable(f"{cmd[0]!r} not on PATH: {e}") from e
            raise DaemonUnavailable(str(e)) from e
        except subprocess.TimeoutExpired as e:
            if retry:
                return self._spawn_url(retry=False)
            raise DaemonUnavailable(f"{cmd[0]} url timed out") from e
        if proc.returncode != 0:
            detail = (proc.stderr or proc.stdout or "").strip() or f"exit {proc.returncode}"
            if retry:
                return self._spawn_url(retry=False)
            raise DaemonUnavailable(detail)
        url = (proc.stdout or "").strip().splitlines()[0:1]
        if not url:
            if retry:
                return self._spawn_url(retry=False)
            raise DaemonUnavailable("empty stdout from browser-daemon url")
        self._cached_url = url[0]
        return self._cached_url

    # ---- Active tab (US1) ------------------------------------------------

    def active_tab(self) -> Optional[dict]:
        """Return the user's currently-focused real tab, or None if unavailable.

        Spec §D.7 shows this as best-effort: any failure → None, and the caller
        (``current_page()``) falls back to ``list_tabs()[0]``.
        """
        cmd = [self._daemon_bin, "active-tab", "--json"]
        # ``active-tab`` opens a transient CDP ws on the upstream Chrome to
        # learn the most-recent ``Target.activateTarget``. On a cold daemon
        # with autoconnect that includes Chrome's Allow dialog wait, so 2s
        # was too tight in practice.
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=8)
        except (FileNotFoundError, subprocess.TimeoutExpired):
            return None
        if proc.returncode != 0 or not proc.stdout.strip():
            return None
        try:
            data = json.loads(proc.stdout)
        except json.JSONDecodeError:
            return None
        # Best effort: ensure expected keys present, fill defaults.
        return {
            "targetId": data.get("targetId"),
            "url": data.get("url", ""),
            "title": data.get("title", ""),
            "accuracy": data.get("accuracy", "unknown"),
            "since_seconds": data.get("since_seconds"),
        }

    # ---- doctor ----------------------------------------------------------

    def doctor(self) -> dict:
        """Forward ``browser-daemon doctor --json``. Always returns a dict; on
        failure returns a synthetic ``schema_version:1`` blob explaining why."""
        cmd = [self._daemon_bin, "doctor", "--json"]
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
        except (FileNotFoundError, subprocess.TimeoutExpired) as e:
            return {
                "schema_version": 1,
                "backends": [],
                "error": str(e),
                "skill_synthetic": True,
            }
        if proc.returncode != 0:
            return {
                "schema_version": 1,
                "backends": [],
                "error": (proc.stderr or proc.stdout or "").strip(),
                "skill_synthetic": True,
                "exit_code": proc.returncode,
            }
        try:
            return json.loads(proc.stdout)
        except json.JSONDecodeError:
            return {
                "schema_version": 1,
                "backends": [],
                "error": "doctor output was not JSON",
                "skill_synthetic": True,
            }

    # ---- API parity stubs (REVIEW.md F-12) -----------------------------

    def disconnect_upstream(self, reason: str = "skill_idle") -> bool:
        """No-op in Mode A — there's no long-lived upstream ws to close.

        ``ModeBClient`` owns the upstream and exposes a real
        ``disconnect_upstream`` so the idle policy can ask the daemon to
        drop its ws (banner disappears). For source-compat with callers
        that hold either client through the ``auto_client()`` factory,
        Mode A returns ``False`` (nothing was disconnected) without
        raising.
        """
        _ = reason
        return False
