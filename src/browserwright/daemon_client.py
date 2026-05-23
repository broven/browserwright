"""Mode A daemon client — subprocess + stdout parsing + single retry.

Talks to the external ``browserwright-daemon`` CLI:

* ``browserwright-daemon url [--backend X]``  → single-line WS URL on stdout
* ``browserwright-daemon active-tab --json``  → {"targetId","url","title","accuracy","since_seconds"}
* ``browserwright-daemon doctor --json``      → schema_version:1 doctor blob

Mock mode (env ``BS_DAEMON_URL_CMD`` or ``BS_CDP_WS``):
  - If ``BS_CDP_WS`` is set, ``resolve_ws_url()`` returns it directly. This lets
    you point Skill at an already-running CDP endpoint (browser-harness daemon,
    Browser Use cloud, raw Chrome --remote-debugging-port=N, etc.) before the
    real ``browserwright-daemon`` binary is available.

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


# Doctor-blob schema_versions this browserwright build can read (daemon current = 2).
_SUPPORTED_DOCTOR_SCHEMAS = (1, 2)


class DaemonClient:
    """Mode A subprocess client.

    Stateless except for a cached ws URL. Methods are sync — they fork
    ``browserwright-daemon`` for each call. Within one heredoc the CDP ws is
    resolved once and re-used via the in-process CDP transport.
    """

    def __init__(
        self,
        url_cmd: Optional[str] = None,
        backend: Optional[str] = None,
        daemon_bin: str = "browserwright-daemon",
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
          3. ``browserwright-daemon url [--backend X]`` subprocess.
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
            raise DaemonUnavailable("empty stdout from browserwright-daemon url")
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
        # learn the most-recent ``Target.activateTarget``. 8s gives slow
        # cold-start cases (e.g. first-time extension attach) headroom.
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
        """Forward ``browserwright-daemon doctor --json``. Always returns a dict; on
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

    # ---- doctor check table (A4) ----------------------------------------

    def doctor_checks(self) -> dict:
        """Derive an actionable ``{status, message, fix}`` check table from the
        raw ``doctor()`` blob (A4).

        Each check is ``{"name", "status", "message", "fix"}`` where status is
        one of ``pass`` / ``warn`` / ``fail``. The discipline (enforced by the
        gate test): **every ``fail`` check carries a non-empty ``fix``**. The
        ``fix`` for non-fail checks is the empty string.

        This is a pure transform over the daemon blob plus a couple of local
        probes (helper-module parse), so it stays deterministic and testable
        without a live browser. Checks whose ground truth needs a live daemon /
        extension degrade to ``warn`` rather than asserting health they can't
        observe.
        """
        info = self.doctor()
        checks: list[dict] = []

        def add(name, status, message, fix=""):
            # Invariant: a fail must always ship a recovery action.
            if status == "fail" and not (fix and fix.strip()):
                fix = "run `browserwright doctor` and address the first failing check"
            checks.append({"name": name, "status": status,
                           "message": message, "fix": fix})

        # 1. daemon reachable (did `browserwright-daemon doctor` actually answer?)
        if info.get("skill_synthetic"):
            add(
                "daemon",
                "fail",
                info.get("error") or "browserwright-daemon did not respond",
                "install/start the daemon: ensure `browserwright-daemon` is on PATH "
                "then `browserwright-daemon serve --backend <extension|rdp>`",
            )
        else:
            add("daemon", "pass", "browserwright-daemon responded to doctor", "")

        # 2. schema version sanity (catches a daemon too old to speak the blob)
        sv = info.get("schema_version")
        if not info.get("skill_synthetic"):
            # Schemas this browserwright build knows how to read. The daemon's
            # current contract is v2 (bumped in daemon v0.5.3); v1 is still
            # parseable for the fields we use. Anything else = real version skew.
            if sv in _SUPPORTED_DOCTOR_SCHEMAS:
                add("daemon_schema", "pass", f"doctor schema_version={sv}", "")
            else:
                add(
                    "daemon_schema",
                    "warn",
                    f"unexpected doctor schema_version={sv!r}",
                    "update browserwright-daemon and browserwright to matching versions",
                )

        # 3. at least one usable backend (relay/extension/rdp connection probe)
        backends = info.get("backends") or []
        usable = [b for b in backends if b.get("available")]
        if not info.get("skill_synthetic"):
            if usable:
                names = ", ".join(b.get("name", "?") for b in usable)
                add("backend", "pass", f"available backend(s): {names}", "")
            elif backends:
                # backends exist but none available — surface each one's hint.
                hints = [b.get("needs_user_action") for b in backends
                         if b.get("needs_user_action")]
                add(
                    "backend",
                    "fail",
                    "no backend is available "
                    f"(saw: {', '.join(b.get('name', '?') for b in backends)})",
                    "; ".join(hints) if hints else
                    "connect the extension (load unpacked) or start an rdp Chrome, "
                    "then re-run doctor",
                )
            else:
                add(
                    "backend",
                    "fail",
                    "daemon reported no backends",
                    "start a backend: load the extension or "
                    "`browserwright-daemon serve --backend rdp`",
                )

        # 4. extension/relay specific: if an extension backend exists but is
        #    unavailable, call it out as its own actionable check.
        ext = next((b for b in backends if b.get("name") == "extension"), None)
        if ext is not None:
            if ext.get("available"):
                add("extension", "pass",
                    f"extension connected (ws={ext.get('ws_url', '')})", "")
            else:
                add(
                    "extension",
                    "warn",
                    ext.get("ux_warning") or "extension backend present but not connected",
                    ext.get("needs_user_action")
                    or "open Chrome and load the unpacked extension, then re-run doctor",
                )

        # 5. helper surface parses (local, deterministic): can we import the
        #    primitive surface agents actually call? A broken install / syntax
        #    error here would otherwise only show up mid-task.
        try:
            from . import api as _api  # noqa: F401
            n = len(getattr(_api, "EXPORTS", []) or [])
            add("helpers", "pass", f"helper surface imports ({n} exports)", "")
        except Exception as e:  # noqa: BLE001
            add(
                "helpers",
                "fail",
                f"helper surface failed to import: {e!r}",
                "reinstall browserwright (`browserwright install`) or check the "
                "traceback above for a syntax/dependency error",
            )

        any_fail = any(c["status"] == "fail" for c in checks)
        return {
            "schema_version": 1,
            "ok": not any_fail,
            "checks": checks,
            "raw": info,
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
