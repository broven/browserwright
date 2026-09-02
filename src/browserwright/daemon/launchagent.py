"""macOS LaunchAgent integration — the daemon as a supervised OS service.

The daemon was originally a one-shot ``serve`` subprocess, but the "zero manual
ops after install" extension flow needs it to be a *service*: started at login,
restarted on crash, reachable on the same socket across reboots. On macOS that
primitive is a LaunchAgent. Linux/systemd-user support is deferred (nobody has
hit it on this codebase yet); the three verbs refuse loudly off darwin rather
than pretending.

There is exactly one global daemon, so there is exactly one label and one
plist — no per-instance name (``BD_NAME`` is retired; see CONTEXT.md).

Everything here returns data or raises :class:`LaunchAgentError`; nothing
prints. The CLI owns stdout/stderr shape and exit codes, and
:exc:`LaunchAgentError` carries the exit code the CLI should use so the error
taxonomy stays with the operation that knows it.
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path
from xml.sax.saxutils import escape as _xml_escape

from .errors import UserError


#: The single global daemon's LaunchAgent label.
LABEL = "com.browserwright-daemon"

#: Where the daemon's stdout/stderr land. Created by :func:`install` before
#: ``launchctl load``, because launchd will not create it for us.
LOG_DIR = "~/.cache/browserwright-daemon/logs"

#: PATH handed to the daemon. LaunchAgents inherit almost nothing, and the
#: daemon spawns Chrome, so both Homebrew layouts (Intel + Apple Silicon) are
#: on it. Constant — never interpolated from user input, so never escaped.
_ENV_PATH = "/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin"


class LaunchAgentError(Exception):
    """A LaunchAgent operation failed. ``exit_code`` is what the CLI returns."""

    def __init__(self, message: str, exit_code: int) -> None:
        super().__init__(message)
        self.exit_code = exit_code


def require_darwin(verb: str) -> None:
    """Raise unless we're on macOS. ``verb`` names the offending subcommand."""
    if sys.platform == "darwin":
        return
    hint = ("; for Linux run `browserwright-daemon serve` from a systemd-user "
            "unit yourself for now") if verb == "install" else ""
    raise LaunchAgentError(
        f"`{verb}` is macOS-only (LaunchAgent){hint}", 1)


def plist_dir() -> Path:
    return Path.home() / "Library" / "LaunchAgents"


def plist_path() -> Path:
    return plist_dir() / f"{LABEL}.plist"


def is_installed() -> bool:
    return plist_path().exists()


def resolve_daemon_bin() -> str:
    """Absolute path to the ``browserwright-daemon`` console script.

    The plist needs it fully qualified — LaunchAgents don't inherit the user's
    shell PATH, so a bare name would never resolve.
    """
    import shutil
    path = shutil.which("browserwright-daemon")
    if path:
        return path
    candidate = Path(sys.prefix) / "bin" / "browserwright-daemon"
    if candidate.exists():
        return str(candidate)
    raise UserError(
        "browserwright-daemon binary not found on PATH; "
        "install it via pip/uv before running `browserwright-daemon install`"
    )


def build_plist(*, extension_port: int | None,
                facade_host: str | None = None,
                facade_port: int | None = None) -> str:
    """Emit the plist content.

    Hand-written rather than via ``plistlib`` because the schema is fixed and
    tiny and this keeps the exact key order readable in a diff. Every
    interpolated value passes through ``xml.sax.saxutils.escape``.

    Pure: no filesystem side effects. :func:`install` creates :data:`LOG_DIR`
    separately so this stays unit-testable.
    """
    bin_path = resolve_daemon_bin()
    args = [bin_path, "serve"]
    if extension_port is not None:
        args += ["--extension-port", str(extension_port)]
    if facade_port is not None:
        args += ["--facade-port", str(facade_port)]
    if facade_host is not None:
        args += ["--facade-host", str(facade_host)]
    log_dir = os.path.expanduser(LOG_DIR)
    stdout_path = f"{log_dir}/browserwright-daemon.stdout.log"
    stderr_path = f"{log_dir}/browserwright-daemon.stderr.log"
    arg_xml = "\n        ".join(
        f"<string>{_xml_escape(a)}</string>" for a in args
    )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" '
        '"http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
        '<plist version="1.0">\n'
        '<dict>\n'
        f'    <key>Label</key><string>{_xml_escape(LABEL)}</string>\n'
        '    <key>ProgramArguments</key>\n'
        '    <array>\n'
        f'        {arg_xml}\n'
        '    </array>\n'
        '    <key>RunAtLoad</key><true/>\n'
        '    <key>KeepAlive</key>\n'
        # Revive on a clean exit AND on a crash (issue #39). The two conditions
        # are OR-ed by launchd.
        #
        # CAREFUL — this is NOT "always keep alive", which is what this comment
        # used to claim. Per launchd.plist(5), `Crashed` means *terminated by a
        # signal*, not "exited non-zero". Measured on macOS 26.1 with exactly
        # this dict: exit 0 -> `runs = 2` (revived); exit 1 -> `runs = 1`,
        # `state = not running` (NOT revived, ever). So the one path that exits
        # non-zero without a signal — `serve`'s "already running (pid N)"
        # self-deferral in listener.py — leaves the job permanently dead. That
        # gap is why `restart` stops the incumbent before bouncing instead of
        # trusting launchd to converge (issue #57); closing it in the plist
        # would re-open the #15 crash-loop question, so it is deliberately left
        # open and covered on the `restart` side.
        #
        # The two conditions are deliberate:
        #   - SuccessfulExit=false once classified a clean exit-0 (graceful
        #     `stop`, the issue #15 control-socket watchdog self-exit) as
        #     "job finished" and launchd never revived the global daemon —
        #     permanent silent death until a human ran `restart`.
        #   - Crashed=true is what revives a crash. The #15 crash-loop (a
        #     half-alive port-holding daemon bouncing on EADDRINUSE) is NOT
        #     prevented here — launchd throttles respawns (~10s) and `serve`
        #     heals the cause by reclaiming stale ports before binding
        #     (issue #15, 2.2), so a revival converges instead of looping.
        '    <dict>\n'
        '        <key>SuccessfulExit</key><true/>\n'
        '        <key>Crashed</key><true/>\n'
        '    </dict>\n'
        '    <key>EnvironmentVariables</key>\n'
        '    <dict>\n'
        f'        <key>PATH</key><string>{_ENV_PATH}</string>\n'
        '    </dict>\n'
        f'    <key>StandardOutPath</key><string>{_xml_escape(stdout_path)}</string>\n'
        f'    <key>StandardErrorPath</key><string>{_xml_escape(stderr_path)}</string>\n'
        f'    <key>WorkingDirectory</key><string>{_xml_escape(os.path.expanduser("~"))}</string>\n'
        '</dict>\n'
        '</plist>\n'
    )


def launchctl(*args: str) -> tuple[int, str, str]:
    """Run ``launchctl <args>``; return ``(rc, stdout, stderr)``.

    A missing binary or a hung call comes back as ``(-1, "", <reason>)`` rather
    than an exception — every caller here treats "launchctl didn't work" as an
    ordinary failure branch.
    """
    import subprocess
    try:
        proc = subprocess.run(["launchctl", *args],
                              capture_output=True, text=True, timeout=10)
        return proc.returncode, proc.stdout, proc.stderr
    except (FileNotFoundError, subprocess.TimeoutExpired) as e:
        return -1, "", str(e)


def install(*, extension_port: int | None,
            facade_host: str | None = None,
            facade_port: int | None = None,
            force: bool = False) -> dict:
    """Write the plist and ``launchctl load -w`` it. Returns the report dict."""
    require_darwin("install")
    path = plist_path()
    if path.exists() and not force:
        raise LaunchAgentError(
            f"{path} already exists. Use --force to replace.", 1)
    carried: dict = {}
    # `--facade-host ""` is the explicit way back to loopback-only: an empty
    # host means "no host argument" and is NOT carried forward from the
    # previous plist.
    reset_host = facade_host is not None and str(facade_host).strip() == ""
    if reset_host:
        facade_host = None
    if path.exists():
        # No flag given → keep what the installed plist says (rule 3).
        previous = plist_serve_args(path)
        if extension_port is None and "extension_port" in previous:
            extension_port = previous["extension_port"]
            carried["extension_port"] = extension_port
        if facade_host is None and not reset_host and "facade_host" in previous:
            facade_host = previous["facade_host"]
            carried["facade_host"] = facade_host
        if facade_port is None and "facade_port" in previous:
            facade_port = previous["facade_port"]
            carried["facade_port"] = facade_port
    path.parent.mkdir(parents=True, exist_ok=True)
    os.makedirs(os.path.expanduser(LOG_DIR), exist_ok=True)
    content = build_plist(extension_port=extension_port,
                          facade_host=facade_host,
                          facade_port=facade_port)
    # --force over an existing plist: unload the old one first so launchctl
    # picks up the new ProgramArguments cleanly. The rc is kept (issue #57 —
    # `restart` used to throw this half away) but is only a warning: on a plist
    # that was never loaded, `unload` "fails" and that is the expected case.
    unload_err = ""
    if path.exists():
        rc_unload, _, unload_err = launchctl("unload", str(path))
        if rc_unload == 0 and not unload_err.strip():
            unload_err = ""
    path.write_text(content)
    rc, _, err = launchctl("load", "-w", str(path))
    if rc != 0:
        # Roll back the write so a re-run isn't blocked by "already exists".
        path.unlink(missing_ok=True)
        raise LaunchAgentError(f"launchctl load failed: {err.strip()}", 3)
    payload = {
        "ok": True,
        "label": LABEL,
        "plist": str(path),
        "extension_port": extension_port,
        "facade_host": facade_host,
        "facade_port": facade_port,
    }
    if carried:
        payload["carried_from_previous_plist"] = carried
    if unload_err.strip():
        payload["unload_warning"] = unload_err.strip()
    return payload


def uninstall() -> dict:
    """``launchctl unload`` and remove the plist. Idempotent."""
    require_darwin("uninstall")
    path = plist_path()
    if not path.exists():
        return {"ok": False, "reason": "no LaunchAgent installed"}
    rc, _, err = launchctl("unload", str(path))
    # Even when unload fails (e.g. it wasn't loaded) we still remove the plist —
    # the user asked for it gone, and leaving it would re-load at next login.
    path.unlink()
    payload = {"ok": True, "removed": str(path)}
    if rc != 0 and err.strip():
        payload["unload_warning"] = err.strip()
    return payload


def service_target() -> str:
    """The modern domain target for our job, e.g. ``gui/501/com.browserwright-…``.

    Used for *reading* job state via ``launchctl print``. The mutations still go
    through ``load``/``unload`` — see :func:`restart` for why that is not the
    bug it looks like.
    """
    return f"gui/{os.getuid()}/{LABEL}"


def job_state() -> dict:
    """``{"loaded": bool, "pid": int | None, "state": str | None}`` for our job.

    Parsed from ``launchctl print``, which is the only launchctl surface that
    reports a pid. ``launchctl list`` shows one too, but pads its output
    differently across releases and says nothing about ``state``.

    A job that is loaded but has no live process reports ``pid=None`` with a
    ``state`` like ``not running`` — that distinction is the whole point of
    reading this, so don't collapse the two.
    """
    rc, out, _err = launchctl("print", service_target())
    if rc != 0:
        return {"loaded": False, "pid": None, "state": None}
    pid: int | None = None
    state: str | None = None
    for line in out.splitlines():
        stripped = line.strip()
        if pid is None and stripped.startswith("pid = "):
            try:
                pid = int(stripped[len("pid = "):].strip())
            except ValueError:
                pid = None
        elif state is None and stripped.startswith("state = "):
            state = stripped[len("state = "):].strip()
    return {"loaded": True, "pid": pid, "state": state}


def _log_tail(lines: int = 12) -> str:
    """Last few lines of the daemon's stderr log, for a failure report.

    The daemon narrates its own refusals there ("already running (pid N);
    running 0.8.2, installed 0.9.0") — that line names the culprit, and before
    issue #57 nothing ever read it back to the operator.
    """
    log_path = Path(os.path.expanduser(LOG_DIR)) / "browserwright-daemon.stderr.log"
    try:
        with open(log_path, "rb") as fh:
            # Seek from the end rather than reading the file — this log is
            # multi-megabyte on a machine that has been up for a while.
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            window = min(size, 8192)
            fh.seek(size - window)
            text = fh.read().decode("utf-8", "replace")
    except OSError:
        return ""
    tail = [ln for ln in text.splitlines() if ln.strip()][-lines:]
    return "\n".join(tail)


def plist_program(path: Path) -> str | None:
    """``ProgramArguments[0]`` from an installed plist, or None if unreadable."""
    import plistlib

    try:
        data = plistlib.loads(path.read_bytes())
        args = data["ProgramArguments"]
    except Exception:  # noqa: BLE001 — a hand-edited plist must not crash restart
        return None
    if isinstance(args, list) and args and isinstance(args[0], str):
        return args[0]
    return None


def plist_serve_args(path: Path) -> dict:
    """The ``--extension-port`` / ``--facade-host`` / ``--facade-port`` values
    an installed plist passes to ``serve``. Missing keys are absent; an
    unreadable plist yields ``{}``.

    ADR-0012 rule 3: the plist is generated, never hand-edited — so a
    regeneration (`install --force` with no flags) must carry these forward,
    or the tailnet bind that remote use depends on silently disappears.
    """
    import plistlib

    try:
        data = plistlib.loads(path.read_bytes())
        args = list(data["ProgramArguments"])
    except Exception:  # noqa: BLE001 — a damaged plist yields no defaults
        return {}
    out: dict = {}
    names = {"--extension-port": "extension_port",
             "--facade-port": "facade_port",
             "--facade-host": "facade_host"}
    for i, a in enumerate(args):
        key = names.get(str(a))
        if key and i + 1 < len(args):
            raw = str(args[i + 1])
            out[key] = int(raw) if key != "facade_host" and raw.isdigit() else raw
    return out


def expected_version(path: Path, fallback: str) -> str:
    """The version the LaunchAgent's *own* binary reports.

    Not ``__version__``. The process doing the restarting is frequently not the
    process the LaunchAgent runs: `mise run restart-daemon` from a checkout
    executes the worktree's `browserwright-daemon`, while the plist points at the
    globally installed one. Verifying against the caller's version would then
    fail a restart that in fact succeeded perfectly — and, because we stop the
    incumbent first, it would fail *after* having torn the daemon down.

    Asking the configured binary costs one ~200ms subprocess and is the only
    answer that is right in both cases: after an upgrade it is the newly
    installed version (which is exactly the drift issue #57 is about), and from a
    checkout it is whatever the global install provides.
    """
    program = plist_program(path)
    if not program:
        return fallback
    import subprocess

    try:
        proc = subprocess.run([program, "version"], capture_output=True,
                              text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return fallback
    if proc.returncode != 0:
        return fallback
    # `browserwright-daemon version` prints "browserwright-daemon X.Y.Z".
    parts = proc.stdout.strip().split()
    return parts[-1] if parts else fallback


def _live_daemon_version(cfg) -> str | None:
    """The version the *running* daemon reports, or None if nothing answers.

    Deliberately the relay's `/__status__` and not the binary on disk: the whole
    of issue #57 is the gap between those two answers.
    """
    from .relay_status import fetch_json

    host, port = cfg.backends.extension.resolved_host_port()
    payload = fetch_json(host, port, timeout=1.0)
    if not payload:
        return None
    version = payload.get("daemon_version")
    return str(version) if version else None


def _stop_incumbent(timeout: float) -> dict:
    """Stop whatever currently answers our endpoint. Mirrors `_cmd_stop`.

    This used to be safe by construction: the control socket lived under *our*
    runtime dir, so anything answering it was our daemon by definition. ADR-0011
    replaced that socket with a URL, and a URL can name another machine — where
    the pid on the pong is a pid over there. The caller (:func:`restart`) is a
    LaunchAgent operation on this machine anyway, so an endpoint that is not
    locally signalable (see `DaemonEndpoint.is_locally_signalable`) is refused
    outright rather than signalled. A LOCAL daemon bound to a tailnet IP stays
    stoppable — it publishes its endpoint into our runtime dir, which is what
    makes it ours.

    (Issue #44 B is a different case — the relay *ports*, where a stranger's
    daemon can hold 19989 without being ours. That one is caught by the version
    check in :func:`restart`'s verification, where refusing is right.)
    """
    from . import _ipc, platforms, supervise
    from ..daemon_url import daemon_endpoint, not_ours_to_signal_message

    endpoint = daemon_endpoint()
    if not endpoint.is_locally_signalable:
        raise LaunchAgentError(
            not_ours_to_signal_message(endpoint, "restart"), exit_code=3)

    pid = _ipc.ping_sync(timeout=1.0)
    if pid is None:
        return {"stopped": None}
    start0 = platforms.proc_start_time(pid)

    def _same_process() -> bool:
        # PID-reuse guard, same contract as `stop`: an unverifiable platform
        # degrades to "signal anyway" rather than refusing a legitimate stop.
        if start0 is None:
            return True
        return platforms.proc_start_time(pid) == start0

    supervise.terminate(
        pid,
        is_dead=lambda: _ipc.ping_sync(timeout=0.3) is None,
        grace=timeout,
        kill_grace=0,
        interval=0.1,
        guard=_same_process,
    )
    return {"stopped": pid}


def restart(cfg, *, force: bool = False, timeout: float = 5.0) -> dict:
    """Replace the running daemon with the installed one, and *prove* it.

    Issue #57: this used to be `unload` + `load` + `return {"ok": True}`, where
    `ok` meant only "`launchctl load` exited 0". It did not mean a process was
    stopped, that a new one started, or that the new one was running new code —
    and `upgrade-global` reported a completely successful upgrade while the
    0.8.2 daemon kept serving.

    Three things make the naive version unfixable by tightening return codes:

    1. **The legacy shims exit 0 on failure.** Measured on macOS 26.1:
       `launchctl load` on an already-loaded job prints ``Load failed: 5:
       Input/output error`` to stderr and still exits 0; `unload` on a job that
       is not loaded does the same. So `if rc != 0` can essentially never fire.
       (`load`/`unload` themselves are *not* the bug — on the same machine they
       replace the process correctly, `kickstart -k` included. Switching verbs
       would have changed nothing.)
    2. **`serve` defers to an incumbent.** A freshly launched daemon that finds
       another one on the control socket prints "already running (pid N)" and
       exits 1 — so a bounce that races a still-live incumbent is a silent
       no-op, and launchd does not revive an exit-1 job (see the KeepAlive note
       in :func:`build_plist`). That is why we stop the incumbent *first*
       instead of hoping the reload wins the race.
    3. **A new process is not new code.** `uv tool install --force` rewrites the
       tool tree under a running daemon; a daemon revived inside that window
       imports whatever is on disk at that instant and holds it for life. Only
       the *version the live process reports* settles it.

    So the post-condition is all three: the job has a pid, it is a different pid,
    and `/__status__` reports our version. Anything else raises.

    ``force`` overrides the "someone is working" gate (:mod:`restart_guard`) and
    nothing else — it never makes us signal a daemon that is not ours.
    """
    require_darwin("restart")
    path = plist_path()
    if not path.exists():
        raise LaunchAgentError(
            "no LaunchAgent installed; run `browserwright-daemon install` "
            "or restart your foreground `serve` process manually", 2)

    from . import restart_guard
    from .. import __version__
    # The LaunchAgent's binary, not the checkout invoking this command, defines
    # the version a healthy incumbent must report (issue #57).
    want = expected_version(path, __version__)

    # ADR-0013 rule 5: restart is gated on DIAGNOSIS, not on session count.
    # A daemon that answers its ping with the installed version is healthy;
    # restarting it fixes nothing an agent could be seeing, and it is the
    # move that on 2026-09-01 took out another agent's session. Name the
    # per-session states and the verb that acts on them instead.
    if not force:
        verdict = daemon_self_check(cfg, expected_version=want)
        if verdict["healthy"]:
            states = _session_states_text(cfg)
            raise LaunchAgentError(
                f"refusing to restart: {verdict['detail']}. A healthy daemon "
                "is not what is wrong.\n"
                f"{states}"
                "For a broken session run `browserwright recover --session "
                "<id>`; `browserwright doctor` names the broken layer.", 4)
    activity = restart_guard.probe(cfg)
    if activity.blocked and not force:
        detail = "\n".join(f"  - {r}" for r in activity.reasons)
        raise LaunchAgentError(
            "refusing to restart: the daemon needs replacement but is in use; "
            "interrupting a mid-call session is not automatic.\n"
            f"{detail}\n"
            "Wait for the calls to finish (`browserwright-daemon activity`), "
            "then run the command again. A forced interruption is a human "
            "decision, not a recovery instruction.", 4)

    # Resolve what "up to date" means BEFORE tearing anything down, so a plist
    # we cannot interrogate fails fast instead of after the daemon is already
    # stopped.
    before = job_state()
    from . import _ipc
    _ipc.log_lifecycle(
        "restart", pid_before=before.get("pid"), forced=bool(force),
        initiator=_ipc.describe_initiator("restart"))
    incumbent_pid = before.get("pid")
    if isinstance(incumbent_pid, int):
        _ipc.request_executor_handoff(incumbent_pid)
    stopped = _stop_incumbent(timeout)

    launchctl("unload", str(path))
    # rc is deliberately not checked — see (1) in the docstring. The verification
    # loop below is the only thing that can tell us whether this worked.
    launchctl("load", "-w", str(path))

    deadline = time.monotonic() + max(timeout, 1.0)
    last: dict = {}
    live_version: str | None = None
    while time.monotonic() < deadline:
        last = job_state()
        pid = last.get("pid")
        if pid is not None and pid != before.get("pid"):
            live_version = _live_daemon_version(cfg)
            if live_version == want:
                return {
                    "ok": True,
                    "restarted": str(path),
                    "pid_before": before.get("pid"),
                    "pid_after": pid,
                    "daemon_version": live_version,
                    "stopped_incumbent": stopped.get("stopped"),
                    "forced": bool(force),
                    "interrupted": list(activity.reasons) if force else [],
                }
        time.sleep(0.2)

    raise LaunchAgentError(_restart_failure_message(
        before=before, after=last, live_version=live_version,
        expected=want, timeout=timeout), 3)


def daemon_self_check(cfg, *, expected_version: str | None = None) -> dict:
    """The three daemon diagnoses of ADR-0013 rule 2, each confirmed by TWO
    consecutive probes so a transient glitch never triggers replacement.

    Returns ``{"healthy": bool, "criterion": None|"gone"|"foreign"|
    "version", "detail": str, "probes": [kind, kind]}``. ``healthy`` is True
    only when both probes found our daemon on the expected version. ``gone``
    and ``version`` permit automatic replacement; ``foreign`` is reported for
    human resolution because an unidentified process is never signalled.
    """
    from . import _ipc
    from .. import __version__
    from ..daemon_url import DaemonEndpoint, daemon_endpoint
    from .config import LOOPBACK_HOST, needs_loopback_cobind

    want = expected_version or __version__
    if cfg is None or not hasattr(cfg, "resolved_facade_port"):
        ep = daemon_endpoint()
    else:
        host = str(getattr(cfg, "facade_host", LOOPBACK_HOST) or LOOPBACK_HOST)
        # Wildcards and specific remote binds are locally reached through the
        # co-bound loopback listener; a configured loopback/hostname is used
        # verbatim.  The port must come from THIS command's cfg, never a stale
        # global endpoint state file belonging to another dev instance.
        probe_host = LOOPBACK_HOST if (
            needs_loopback_cobind(host) or host in ("0.0.0.0", "::", "::0", "*")) else host
        port = int(cfg.resolved_facade_port())
        ep = DaemonEndpoint(url=f"http://{probe_host}:{port}",
                            explicit=True, source="restart_config")
    probes = [_ipc.probe_endpoint_sync(ep.host, ep.port, timeout=1.5)
              for _ in range(2)]
    kinds = [p.kind for p in probes]

    def _classification(probe) -> tuple[str, str | None]:
        if probe.kind == "ours":
            return "ours", probe.version
        if probe.kind in ("foreign", "garbage", "timeout"):
            return "foreign", None
        return "gone", None

    conclusions = [_classification(p) for p in probes]
    if conclusions[0] != conclusions[1]:
        return {"healthy": False, "criterion": None,
                "detail": ("two consecutive probes disagree "
                           f"({conclusions[0]} then {conclusions[1]}); "
                           "nothing is concluded from that"),
                "probes": kinds}
    p = probes[1]
    if p.kind == "ours":
        if p.version != want:
            return {"healthy": False, "criterion": "version",
                    "detail": (f"the daemon at {ep.host}:{ep.port} runs "
                               f"{p.version}, installed is {want}"),
                    "probes": kinds}
        return {"healthy": True, "criterion": None,
                "detail": (f"the daemon answers at {ep.host}:{ep.port} "
                           f"(pid {p.pid}, version {p.version or 'unknown'}) "
                           "on both probes"),
                "probes": kinds}
    if p.kind in ("foreign", "garbage", "timeout"):
        return {"healthy": False, "criterion": "foreign",
                "detail": p.describe(), "probes": kinds}
    return {"healthy": False, "criterion": "gone",
            "detail": p.describe(), "probes": kinds}


def _session_states_text(cfg) -> str:
    """Per-session recovery states from the running daemon, one line each,
    for the restart refusal. Empty when the daemon cannot be asked."""
    from . import restart_guard
    snap = restart_guard._fetch_snapshot(cfg, timeout=2.0)  # noqa: SLF001
    rows = (snap or {}).get("sessions") or []
    lines = []
    for row in rows:
        rec = row.get("recovery") or {}
        if rec:
            lines.append(f"  - session {row.get('session_id')}: {rec.get('state')}"
                         + (f" ({rec.get('reason')})" if rec.get("reason") else ""))
    return ("Session states:\n" + "\n".join(lines) + "\n") if lines else ""


def _restart_failure_message(*, before: dict, after: dict,
                             live_version: str | None,
                             expected: str, timeout: float) -> str:
    """Name the specific way the restart failed, not just "it failed".

    Each branch below was a real observed outcome in issue #57, and they call
    for different fixes — collapsing them into one message is how the original
    bug stayed invisible for a whole release.
    """
    pid_after = after.get("pid")
    if not after.get("loaded"):
        return ("restart failed: the LaunchAgent job is not loaded after "
                "`launchctl load`. Re-run `browserwright-daemon install "
                "--force`.")
    if pid_after is None:
        tail = _log_tail()
        hint = f"\nLast daemon log lines:\n{tail}" if tail else ""
        return (f"restart failed: the job is loaded but has no running process "
                f"(state={after.get('state')!r}). The daemon exited on startup; "
                f"launchd does not revive a non-zero exit.{hint}")
    if pid_after == before.get("pid"):
        tail = _log_tail()
        hint = f"\nLast daemon log lines:\n{tail}" if tail else ""
        return (f"restart failed: pid {pid_after} is unchanged after "
                f"{timeout:g}s — nothing was replaced.{hint}")
    if live_version is None:
        return (f"restart failed: a new process started (pid {pid_after}) but "
                f"nothing answered `/__status__` within {timeout:g}s.")
    return (f"restart failed: a new process started (pid {pid_after}) but the "
            f"daemon answering `/__status__` reports {live_version}, not "
            f"{expected}. Either it is still loading old code, or the relay "
            f"port is held by a daemon that is not ours (issue #44 B) — this "
            f"command will not signal that one.")
