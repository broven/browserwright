"""Skill-side health: forward ``browserwright-daemon doctor`` and derive an
actionable ``{status, message, fix}`` check table (A4).

This is **not** a CDP driving path — it shells out to the daemon's standalone
``doctor`` subcommand (zero ws side effects, spec H3) and transforms the blob.
It lived on the old Mode A ``DaemonClient`` historically; it has no dependency
on Mode A and stays after Mode A's removal. Consumed by ``browserwright doctor``
(``cli.py``) and the install wizard's option-availability probe (``install.py``).
"""
from __future__ import annotations

import json
import subprocess

# Doctor blobs this browserwright build knows how to read. The daemon's current
# contract is v2 (bumped in daemon v0.5.3); v1 is still parseable for the fields
# we use. Anything else = real version skew.
_SUPPORTED_DOCTOR_SCHEMAS = (1, 2)


def daemon_doctor() -> dict:
    """Forward ``browserwright-daemon doctor --json``. Always returns a dict; on
    failure returns a synthetic ``schema_version:1`` blob explaining why."""
    cmd = ["browserwright-daemon", "doctor", "--json"]
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


def doctor_checks() -> dict:
    """Derive an actionable ``{status, message, fix}`` check table from the raw
    ``daemon_doctor()`` blob (A4).

    Each check is ``{"name", "status", "message", "fix"}`` where status is one
    of ``pass`` / ``warn`` / ``fail``. The discipline (enforced by the gate
    test): **every ``fail`` check carries a non-empty ``fix``**. The ``fix`` for
    non-fail checks is the empty string.

    This is a pure transform over the daemon blob plus a couple of local probes
    (helper-module parse), so it stays deterministic and testable without a live
    browser. Checks whose ground truth needs a live daemon / extension degrade
    to ``warn`` rather than asserting health they can't observe.
    """
    info = daemon_doctor()
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
            "then `browserwright-daemon serve`",
        )
    else:
        add("daemon", "pass", "browserwright-daemon responded to doctor", "")

    # 2. schema version sanity (catches a daemon too old to speak the blob)
    sv = info.get("schema_version")
    if not info.get("skill_synthetic"):
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
                "create an rdp session after `browserwright-daemon serve`",
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

    # Backend-specific warnings should not hide in raw output. Surface every
    # warning as a top-level check so human output and JSON consumers both see
    # version skew / schema mismatch / UX warnings even when a backend is
    # otherwise available.
    for b in backends:
        warning = b.get("ux_warning")
        if not warning:
            continue
        name = f"{b.get('name', 'backend')}_warning"
        if any(c.get("name") == name and c.get("message") == warning for c in checks):
            continue
        add(
            name,
            "warn",
            warning,
            b.get("needs_user_action")
            or "update browserwright-daemon, browserwright, and the Chrome extension to matching versions",
        )

    # 5. helper surface parses (local, deterministic): can we import the
    #    primitive surface agents actually call? A broken install / syntax
    #    error here would otherwise only show up mid-task.
    try:
        import browserwright as _bw  # noqa: F401
        n = len(getattr(_bw, "EXPORTS", []) or [])
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
