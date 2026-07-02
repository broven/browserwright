"""Platform-specific Chrome / Chromium-family profile locations.

This table is sourced verbatim from browser-harness `daemon.py:36-65` — the spec
(§8.3) is explicit: "this table is fought for, do not reinvent it." Any addition
should match a real installed browser and ship with a smoke test.

Covers macOS, Linux, Linux Flatpak × Chrome (Stable/Canary) / Chromium
/ Edge (Stable/Beta/Dev/Canary) / Brave / Arc / Dia / Comet.
"""
from __future__ import annotations

import os
import platform
import shutil
import subprocess
from pathlib import Path


def proc_start_time(pid: int) -> str | None:
    """Best-effort process start-time fingerprint, used to detect PID reuse
    before signalling a daemon (see ``cli._cmd_stop``).

    Returns an opaque but *stable* string identifying when the process started,
    or ``None`` when the platform can't answer (no such pid, or unsupported) —
    callers must treat ``None`` as "can't verify" and fall back, never as a
    match.

    - Linux: field 22 (``starttime``, in clock ticks since boot) of
      ``/proc/<pid>/stat``.
    - macOS / BSD: ``ps -o lstart= -p <pid>`` (a stable wall-clock string).
    """
    # Linux fast path — no subprocess.
    try:
        stat = Path(f"/proc/{pid}/stat")
        if stat.exists():
            data = stat.read_text()
            # comm (field 2) is parenthesised and may contain spaces/parens;
            # split after the final ')' so positional fields stay aligned.
            rparen = data.rfind(")")
            if rparen != -1:
                # After "pid (comm) " the remaining fields start at state
                # (field 3). starttime is field 22 → index 22 - 3 = 19.
                fields = data[rparen + 2:].split()
                if len(fields) > 19:
                    return fields[19]
    except (OSError, ValueError, IndexError):
        pass
    # macOS / BSD — ask ps for the start timestamp.
    try:
        out = subprocess.run(
            ["ps", "-o", "lstart=", "-p", str(pid)],
            capture_output=True, text=True, timeout=3.0)
        if out.returncode == 0:
            s = out.stdout.strip()
            return s or None
    except (OSError, subprocess.SubprocessError):
        pass
    return None


def profile_paths() -> list[Path]:
    """The full cross-platform profile list. Order is significant only as
    documentation — callers should always tie-break by mtime, never by order.
    """
    home = Path.home()
    return [
        # macOS
        home / "Library/Application Support/Google/Chrome",
        home / "Library/Application Support/Google/Chrome Canary",
        home / "Library/Application Support/Comet",
        home / "Library/Application Support/Arc/User Data",
        home / "Library/Application Support/Dia/User Data",
        home / "Library/Application Support/Microsoft Edge",
        home / "Library/Application Support/Microsoft Edge Beta",
        home / "Library/Application Support/Microsoft Edge Dev",
        home / "Library/Application Support/Microsoft Edge Canary",
        home / "Library/Application Support/BraveSoftware/Brave-Browser",
        # Linux (native)
        home / ".config/google-chrome",
        home / ".config/chromium",
        home / ".config/chromium-browser",
        home / ".config/microsoft-edge",
        home / ".config/microsoft-edge-beta",
        home / ".config/microsoft-edge-dev",
        # Linux (Flatpak)
        home / ".var/app/org.chromium.Chromium/config/chromium",
        home / ".var/app/com.google.Chrome/config/google-chrome",
        home / ".var/app/com.brave.Browser/config/BraveSoftware/Brave-Browser",
        home / ".var/app/com.microsoft.Edge/config/microsoft-edge",
    ]


# Likely-binary paths for `launch-chrome` (§5.5 step 1 fallback). PATH lookup is
# tried first; this list is only consulted if PATH yields nothing.
def chrome_binary_candidates() -> list[Path]:
    system = platform.system()
    if system == "Darwin":
        return [
            Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
            Path("/Applications/Google Chrome Canary.app/Contents/MacOS/Google Chrome Canary"),
            Path("/Applications/Chromium.app/Contents/MacOS/Chromium"),
            Path("/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge"),
            Path("/Applications/Brave Browser.app/Contents/MacOS/Brave Browser"),
        ]
    # Linux + everything else
    return [
        Path("/usr/bin/google-chrome"),
        Path("/usr/bin/google-chrome-stable"),
        Path("/usr/bin/chromium"),
        Path("/usr/bin/chromium-browser"),
        Path("/usr/bin/microsoft-edge"),
        Path("/usr/bin/brave-browser"),
        Path("/snap/bin/chromium"),
    ]


def _binary_is_runnable(path: Path, timeout: float = 3.0) -> bool:
    """Validate a Chrome candidate by `<binary> --version` returning exit 0.

    Task #12: macOS Homebrew installs of `chromium` sometimes leave a wrapper
    shell script at `/opt/homebrew/bin/chromium` that exec's a non-existent
    `.app` and exits 126. PATH-lookup `shutil.which("chromium")` happily
    picks that up and `launch_chrome` falls into the now-fixed Bug #2 poll
    race symptom WITHOUT the underlying poll-race actually being present.

    Cheap (`--version` returns < 50ms in steady state) + side-effect-free
    (no `--user-data-dir` involved). On a healthy install, all browser
    binaries respond to `--version`. We don't parse the output — just check
    the exit code.
    """
    import subprocess
    try:
        result = subprocess.run(
            [str(path), "--version"],
            capture_output=True,
            timeout=timeout,
        )
    except (FileNotFoundError, PermissionError, subprocess.TimeoutExpired):
        return False
    except OSError:
        return False
    return result.returncode == 0


def discover_chrome_binary(explicit: str | None = None) -> Path | None:
    """Implements §5.5 step 1: --chrome-binary > BD_CHROME_BINARY (caller passes
    in via `explicit`) > platform default `.app` list (macOS) > $PATH walk
    with `--version` validation.

    Returns the resolved absolute Path or None when nothing matches. Caller
    decides whether to raise ChromeBinaryNotFound — keeping the policy out here
    lets unit tests assert on (binary_path is None) without depending on $HOME.

    Order rationale (Task #12 fix):
    - macOS, prefer the real `.app` bundle paths from
      `chrome_binary_candidates()` BEFORE `shutil.which` PATH lookup.
      Reason: Homebrew leaves stale wrapper scripts on PATH that exit 126
      (= Bug #2 symptom without the actual poll race).
    - Linux, PATH is the canonical install signal; check it first
      but still validate via `--version` so partial / broken installs fail
      fast with a clean error.
    """
    if explicit:
        p = Path(explicit).expanduser()
        return p if p.exists() and _binary_is_runnable(p) else None

    candidates: list[Path] = []
    if platform.system() == "Darwin":
        # macOS: real .app paths first, then PATH fallback.
        candidates.extend(chrome_binary_candidates())
        for name in ("google-chrome", "google-chrome-stable", "chromium",
                     "chromium-browser", "microsoft-edge", "brave-browser"):
            if (which := shutil.which(name)):
                candidates.append(Path(which))
    else:
        # Linux: PATH first, then platform defaults.
        for name in ("google-chrome", "google-chrome-stable", "chromium",
                     "chromium-browser", "microsoft-edge", "brave-browser"):
            if (which := shutil.which(name)):
                candidates.append(Path(which))
        candidates.extend(chrome_binary_candidates())

    seen: set[Path] = set()
    for p in candidates:
        if p in seen:
            continue
        seen.add(p)
        if not p.exists():
            continue
        if _binary_is_runnable(p):
            return p
    return None


def runtime_dir() -> Path:
    """Where pid / sock files live. Spec §5.5 step 7 + §6.7.

    Mirrors browser-harness _ipc.py logic — XDG_RUNTIME_DIR, /tmp fallback
    (gettempdir() returns long /var/folders on macOS which is unsafe for
    AF_UNIX sun_path's 104-byte budget).
    """
    if (xdg := os.environ.get("XDG_RUNTIME_DIR")):
        return Path(xdg)
    return Path("/tmp")


def cache_dir() -> Path:
    """Where persistent launch-chrome profiles live (§5.5 step 2)."""
    if (xdg := os.environ.get("XDG_CACHE_HOME")):
        return Path(xdg) / "browserwright-daemon"
    return Path.home() / ".cache" / "browserwright-daemon"
