"""Skill-side health: forward ``browserwright-daemon doctor`` and derive an
actionable ``{status, message, fix}`` check table (A4).

This is **not** a CDP driving path — it shells out to the daemon's standalone
``doctor`` subcommand (zero ws side effects, spec H3) and transforms the blob.
It lived on the old Mode A ``DaemonClient`` historically; it has no dependency
on Mode A and stays after Mode A's removal. Consumed by ``browserwright doctor``
(``cli.py``) and the install wizard's option-availability probe (``install.py``).

Daemon health is two separate checks (issue #28): ``daemon_cli`` — is the
``browserwright-daemon`` **binary** reachable and did it answer doctor — and
``daemon_running`` — is the daemon **process** actually up, read from the
liveness probe the doctor blob has carried since schema v3
(``alive`` / ``probe_state`` / ``pid``). Before v3 the blob only proved the
CLI worked, which it does with no daemon running, so ``doctor`` reported
``✓ daemon`` on a machine whose daemon was down.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

# Doctor blobs this browserwright build knows how to read. The daemon's current
# contract is v3 (liveness fields added for issue #28, daemon v0.5.x); v1/v2 are
# still parseable for the fields we use. Anything else = real version skew.
_SUPPORTED_DOCTOR_SCHEMAS = (1, 2, 3)

#: LaunchAgent plist path (macOS autostart). When it exists, a down daemon is
#: a *restart*, not a first start — `serve` would fight launchd over the socket.
_LAUNCHAGENT_PLIST = Path.home() / "Library" / "LaunchAgents" \
    / "com.browserwright-daemon.plist"


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


def _launchagent_installed() -> bool:
    """Whether the macOS LaunchAgent plist exists (daemon autostarts on login).

    When it does, a down daemon is a restart, not a first start: a bare
    ``serve`` from a LaunchAgent-managed install fights launchd over the
    socket. Kept as its own probe so tests can pin it either way.
    """
    return _LAUNCHAGENT_PLIST.exists()


def _daemon_fix(info: dict) -> str:
    """Recovery action for a down daemon (issue #28).

    A half-alive daemon (``port_held_by_unresponsive_process``) always needs
    ``restart`` to reclaim its ports. Otherwise: ``restart`` when a LaunchAgent
    is installed (launchd owns the socket), plain ``serve`` when not.
    """
    from .daemon.probe import PORT_HELD

    if info.get("probe_state") == PORT_HELD:
        return "reclaim the daemon's ports: `browserwright-daemon restart`"
    if _launchagent_installed():
        return "restart the LaunchAgent daemon: `browserwright-daemon restart`"
    return "start the daemon: `browserwright-daemon serve`"


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
    synthetic = bool(info.get("skill_synthetic"))

    def add(name, status, message, fix=""):
        # Invariant: a fail must always ship a recovery action.
        if status == "fail" and not (fix and fix.strip()):
            fix = "run `browserwright doctor` and address the first failing check"
        checks.append({"name": name, "status": status,
                       "message": message, "fix": fix})

    # 1. daemon_cli — binary reachability: did `browserwright-daemon doctor`
    #    actually answer? (issue #28: this used to be the *only* daemon check
    #    and was misnamed `daemon`, so a down daemon read as `✓ daemon`.)
    if synthetic:
        add(
            "daemon_cli",
            "fail",
            info.get("error") or "browserwright-daemon did not respond",
            "install/start the daemon: ensure `browserwright-daemon` is on PATH "
            "then `browserwright-daemon serve`",
        )
    else:
        add("daemon_cli", "pass", "browserwright-daemon CLI answered doctor", "")

    # 2. daemon_running — is the daemon *process* up? Read from the liveness
    #    probe the doctor blob has carried since schema v3 (issue #28). A v1/v2
    #    blob lacks it: we can't verify, so warn instead of asserting health.
    if not synthetic:
        if info.get("alive") is False:
            add(
                "daemon_running",
                "fail",
                f"daemon is not running (probe_state={info.get('probe_state')})",
                _daemon_fix(info),
            )
        elif info.get("alive") is True:
            add("daemon_running", "pass",
                f"daemon alive (pid {info.get('pid')})", "")
        else:
            add(
                "daemon_running",
                "warn",
                "cannot verify daemon liveness (doctor schema_version="
                f"{info.get('schema_version')} predates v3 liveness fields)",
                "update browserwright-daemon to match browserwright",
            )

    # 3. facade — the Playwright door. Every `page` / `context` / `snapshot()`
    #    call connects through it, so a live daemon whose facade never bound (or
    #    was disabled) means 100% of browser-driving calls fail. That state used
    #    to be completely invisible here: the discovery file it was published to
    #    got reaped out of /tmp, `doctor` said all green, and every `-e`/`-f`
    #    invocation failed with an unexplained FacadeUnavailable. It is a `fail`,
    #    same tier as a down daemon, because the practical consequence is the
    #    same. Skipped when the daemon is down — that is already reported above,
    #    and one root cause should not print as two failures.
    if not synthetic and info.get("alive") is not False:
        facade = info.get("facade")
        if isinstance(facade, dict) and facade.get("ws"):
            add("facade", "pass",
                f"Playwright facade at {facade['ws']}", "")
        elif "facade" in info:
            reason = (info.get("facade_error")
                      or "the daemon reports no Playwright facade")
            add(
                "facade",
                "fail",
                f"no Playwright facade: {reason}",
                "restart the daemon (`browserwright-daemon restart`); if it was "
                "started with `--facade-port 0`, drop that flag",
            )
        else:
            # A pre-facade doctor blob. Can't observe it, so don't assert it.
            add("facade", "warn",
                "cannot verify the Playwright facade (doctor blob predates the "
                "facade field)",
                "update browserwright-daemon to match browserwright")

    # 4. schema version sanity (catches a daemon too old to speak the blob)
    sv = info.get("schema_version")
    if not synthetic:
        if sv in _SUPPORTED_DOCTOR_SCHEMAS:
            add("daemon_schema", "pass", f"doctor schema_version={sv}", "")
        else:
            add(
                "daemon_schema",
                "warn",
                f"unexpected doctor schema_version={sv!r}",
                "update browserwright-daemon and browserwright to matching versions",
            )

    # 5. at least one usable backend (relay/extension/cdp connection probe)
    backends = info.get("backends") or []
    usable = [b for b in backends if b.get("available")]
    daemon_down = info.get("alive") is False
    if not synthetic:
        if daemon_down:
            # With no daemon running, every backend is unavailable *as a
            # consequence*. Surface a deferral, not independent failures —
            # reporting them independently is what misdirected users away
            # from the daemon (issue #28). The daemon_running check above is
            # the one root-cause failure.
            add("backend", "warn",
                "backend checks deferred: no daemon is running", "")
        elif usable:
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
                "connect the extension (load unpacked) or start an cdp Chrome, "
                "then re-run doctor",
            )
        else:
            add(
                "backend",
                "fail",
                "daemon reported no backends",
                "start a backend: load the extension or "
                "create an cdp session after `browserwright-daemon serve`",
            )

    # 6. extension/relay specific: if an extension backend exists but is
    #    unavailable, call it out as its own actionable check.
    ext = next((b for b in backends if b.get("name") == "extension"), None)
    if ext is not None:
        if ext.get("available"):
            add("extension", "pass",
                f"extension connected (ws={ext.get('ws_url', '')})", "")
        elif daemon_down:
            add(
                "extension",
                "warn",
                "extension backend present but not connected (daemon not running)",
                _daemon_fix(info),
            )
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

    # 7. helper surface parses (local, deterministic): can we import the
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
