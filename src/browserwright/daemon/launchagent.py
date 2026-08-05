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
        '    <dict>\n'
        '        <key>SuccessfulExit</key><false/>\n'
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
    path.parent.mkdir(parents=True, exist_ok=True)
    os.makedirs(os.path.expanduser(LOG_DIR), exist_ok=True)
    content = build_plist(extension_port=extension_port,
                          facade_host=facade_host,
                          facade_port=facade_port)
    # --force over an existing plist: unload the old one first so launchctl
    # picks up the new ProgramArguments cleanly.
    if path.exists():
        launchctl("unload", str(path))
    path.write_text(content)
    rc, _, err = launchctl("load", "-w", str(path))
    if rc != 0:
        # Roll back the write so a re-run isn't blocked by "already exists".
        path.unlink(missing_ok=True)
        raise LaunchAgentError(f"launchctl load failed: {err.strip()}", 3)
    return {
        "ok": True,
        "label": LABEL,
        "plist": str(path),
        "extension_port": extension_port,
        "facade_host": facade_host,
        "facade_port": facade_port,
    }


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


def restart() -> dict:
    """Unload + reload the installed LaunchAgent (post-upgrade bounce)."""
    require_darwin("restart")
    path = plist_path()
    if not path.exists():
        raise LaunchAgentError(
            "no LaunchAgent installed; run `browserwright-daemon install` "
            "or restart your foreground `serve` process manually", 2)
    launchctl("unload", str(path))
    rc, _, err = launchctl("load", "-w", str(path))
    if rc != 0:
        raise LaunchAgentError(f"launchctl load failed: {err.strip()}", 3)
    return {"ok": True, "restarted": str(path)}
