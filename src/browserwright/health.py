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
_SUPPORTED_DOCTOR_SCHEMAS = (1, 2, 3, 4)

#: LaunchAgent plist path (macOS autostart). When it exists, a down daemon is
#: a *restart*, not a first start — `serve` would fight launchd over the socket.
_LAUNCHAGENT_PLIST = Path.home() / "Library" / "LaunchAgents" \
    / "com.browserwright-daemon.plist"


def daemon_doctor() -> dict:
    """Forward ``browserwright-daemon doctor --json``. Always returns a dict; on
    failure returns a synthetic ``schema_version:1`` blob explaining why."""
    cmd = ["browserwright-daemon", "doctor", "--json"]
    try:
        # ADR-0011: report on the daemon THIS process is addressed at, not the
        # one the child CLI would default to.
        from .daemon_url import child_env
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=10,
                              env=child_env())
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


def _launchd_state() -> dict:
    """What launchd knows about the daemon job: ``{state, pid, last_exit}``.

    Parsed from ``launchctl print``; every field is None when launchd cannot
    be asked (not macOS, job not loaded). Kept as its own probe so tests pin
    it without a launchd.
    """
    import os
    import re
    import subprocess

    out: dict = {"state": None, "pid": None, "last_exit": None}
    try:
        res = subprocess.run(
            ["launchctl", "print", f"gui/{os.getuid()}/com.browserwright-daemon"],
            capture_output=True, text=True, timeout=3.0)
    except (OSError, subprocess.SubprocessError):
        return out
    if res.returncode != 0:
        return out
    for line in res.stdout.splitlines():
        line = line.strip()
        if m := re.match(r"state = (\w+)", line):
            out["state"] = out["state"] or m.group(1)
        elif m := re.match(r"pid = (\d+)", line):
            out["pid"] = int(m.group(1))
        elif m := re.match(r"last exit code = (.+)", line):
            out["last_exit"] = m.group(1).strip()
    return out


def _daemon_fix(info: dict) -> str:
    """Recovery action for a down daemon (issue #28, reworked for ADR-0013).

    No branch tells an agent to restart or serve the daemon: a client that
    only knows "not running" has no evidence a restart is the right move, and
    the default endpoint's on-demand spawn (plus the stale-port reclaim it
    runs first, issue #15) is the sanctioned start path. What doctor CAN do
    is say who is responsible for the process and where its last words are.
    """
    from .daemon.probe import PORT_HELD

    if info.get("probe_state") == PORT_HELD:
        return (
            "the daemon's ports are held by a process that does not answer; "
            "the next on-demand start reclaims ports from a stale browserwright "
            "daemon by itself (issue #15). Re-run `browserwright doctor` in a "
            "few seconds; if it is still held, `lsof -nP -iTCP:19990 "
            "-sTCP:LISTEN` names the holder"
        )
    if _launchagent_installed():
        st = _launchd_state()
        detail = ", ".join(
            f"{k}={v}" for k, v in (("state", st.get("state")),
                                    ("pid", st.get("pid")),
                                    ("last exit", st.get("last_exit")))
            if v is not None)
        return (
            "launchd manages the daemon (LaunchAgent installed"
            + (f": {detail}" if detail else "") + ") and respawns it on its "
            "own; read `browserwright-daemon logs` for why the last start "
            "exited before assuming it is stuck"
        )
    return (
        "no LaunchAgent is installed, so nothing keeps the daemon up; "
        "`browserwright install` sets one up, and the default endpoint "
        "starts a daemon on demand for the next command"
    )


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
            "`browserwright-daemon` is not on PATH or did not answer; "
            "`browserwright install` puts both binaries in place",
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

    # 3. cdp surface — the Playwright door. Every `page` / `context` /
    #    `snapshot()` call connects through it. ADR-0011 collapsed it onto the
    #    daemon's one endpoint, so it can no longer be separately absent: a
    #    daemon that could not bind that port does not start. The check
    #    therefore reports the *address* rather than adjudicating existence, and
    #    only fails when a live daemon reports no surface at all — which now
    #    means a daemon too old to speak this shape.
    if not synthetic and info.get("alive") is not False:
        surface = info.get("cdp_surface") or info.get("facade")
        if isinstance(surface, dict) and surface.get("ws"):
            add("cdp_surface", "pass",
                f"cdp surface at {surface['ws']}", "")
        elif "cdp_surface" in info or "facade" in info:
            add(
                "cdp_surface",
                "fail",
                "the daemon reports no cdp surface",
                "the running daemon is older than the installed package "
                "(`browserwright version check`); the next command against "
                "the default endpoint replaces it",
            )
        else:
            # A doctor blob too old to carry it. Can't observe it, so don't
            # assert it.
            add("cdp_surface", "warn",
                "cannot verify the cdp surface (doctor blob predates the "
                "field)",
                "update browserwright-daemon to match browserwright")

    # 3b. endpoint_reachable — can a LOCAL client actually dial the endpoint?
    #     BUG A: check 3 above only *reports* the advertised address, so a
    #     daemon bound to a specific non-loopback host (`--facade-host
    #     <tailnet-ip>`) read as a clean bill of health while every local
    #     client failed with ECONNREFUSED on 127.0.0.1. Doctor has to dial, not
    #     echo — a health check that cannot observe the reported failure is the
    #     gap, not a passing check.
    if not synthetic and info.get("alive") is not False:
        for check in _endpoint_reachability_checks():
            add(**check)

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
                "start a backend: load the extension, or create a cdp session "
                "(`browserwright session new --backend=cdp --create --name=…`)",
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

    # 6b. sessions — ADR-0013: which layer is broken per session. Read from
    #     the daemon's status snapshot when a live daemon carries it.
    sessions = info.get("sessions") if not synthetic else None
    if isinstance(sessions, list):
        described = [s for s in sessions if isinstance(s.get("recovery"), dict)]
        broken = [s for s in sessions
                  if isinstance(s.get("recovery"), dict)
                  and s["recovery"].get("state") not in (None, "healthy")]
        if broken:
            lines = "; ".join(
                f"{s.get('session_id')}={s['recovery'].get('state')} "
                f"since={s['recovery'].get('since')}"
                + (f" reason={s['recovery'].get('reason')}"
                   if s['recovery'].get('reason') else "")
                for s in broken)
            add("sessions", "warn", f"session(s) not healthy: {lines}",
                "`browserwright recover --session <id>` for the ones you use; "
                "the state names the layer (extension / tab / executor)")
        else:
            add("sessions", "pass",
                "; ".join(
                    f"{s.get('session_id')}=healthy "
                    f"since={s['recovery'].get('since')}"
                    for s in described)
                if described else "no sessions tracked", "")

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


def _probe_tcp(host: str, port: int, *, timeout: float = 1.5) -> str | None:
    """``None`` when a TCP connect succeeds, else a short reason."""
    import socket

    try:
        with socket.create_connection((host, port), timeout=timeout):
            return None
    except OSError as e:
        return e.strerror or str(e)


def _endpoint_reachability_checks() -> list[dict]:
    """Dial the resolved endpoint AND loopback; report the divergence.

    Two distinct failures hide behind "the daemon is running":
      - the endpoint the client resolves answers nothing at all;
      - it answers, but loopback (what every client falls back to when the
        endpoint state file is not visible) does not.
    The second is BUG A, and it is invisible unless you actually connect.
    """
    from .daemon_url import daemon_endpoint

    try:
        ep = daemon_endpoint()
    except Exception:  # noqa: BLE001 - doctor must never raise
        return []

    resolved_err = _probe_tcp(ep.host, ep.port)
    if resolved_err is not None:
        from .daemon_url import local_unreachable_fix
        return [{
            "name": "endpoint_reachable",
            "status": "fail",
            "message": (f"nothing answered at {ep.host}:{ep.port} "
                        f"(the endpoint resolved from {ep.source})"),
            "fix": local_unreachable_fix(ep),
        }]

    if ep.is_loopback:
        return [{
            "name": "endpoint_reachable",
            "status": "pass",
            "message": f"endpoint answers at {ep.host}:{ep.port}",
            "fix": "",
        }]

    loopback_err = _probe_tcp("127.0.0.1", ep.port)
    if loopback_err is None:
        return [{
            "name": "endpoint_reachable",
            "status": "pass",
            "message": (f"endpoint answers at {ep.host}:{ep.port} and on "
                        "127.0.0.1"),
            "fix": "",
        }]
    return [{
        "name": "endpoint_reachable",
        "status": "fail",
        "message": (f"the daemon answers at {ep.host}:{ep.port} but NOT on "
                    f"127.0.0.1:{ep.port} ({loopback_err}) — any local client "
                    "that cannot read the endpoint state file will fail to "
                    "connect"),
        "fix": ("point local clients at it with "
                f"`export BW_DAEMON_URL=http://{ep.host}:{ep.port}`; the "
                "durable fix is a LaunchAgent that serves loopback too "
                "(`browserwright-daemon install --facade-host 0.0.0.0`, a "
                "maintainer step — it replaces the running daemon)"),
    }]
