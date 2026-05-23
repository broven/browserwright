"""Reusable daemon + Chrome for Testing launch logic.

Extracted from conftest.py so both v1 fixtures and v2 agent-e2e hooks can
call the same functions without duplicating code.
"""
from __future__ import annotations

import hashlib
import json
import os
import platform
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path

# ---------------------------------------------------------------------------
#  Chrome for Testing discovery
# ---------------------------------------------------------------------------

_CFT_SEARCH_DIRS = [
    Path("/tmp/chrome-for-testing"),
    Path.home() / ".cache" / "puppeteer",
    Path.home() / ".cache" / "chrome-for-testing",
]


def find_cft_binary() -> Path | None:
    """Discover Chrome for Testing binary. Returns None if not found."""
    system = platform.system()
    if system == "Darwin":
        patterns = [
            "chrome/*/chrome-*/Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing",
        ]
    elif system == "Linux":
        patterns = ["chrome/*/chrome-*/chrome"]
    else:
        return None
    for base in _CFT_SEARCH_DIRS:
        if not base.is_dir():
            continue
        for pat in patterns:
            matches = sorted(base.glob(pat))
            if matches:
                return matches[-1]
    return None


def extension_id_from_path(ext_dir: Path) -> str:
    digest = hashlib.sha256(str(ext_dir.resolve()).encode("utf-8")).hexdigest()[:32]
    return "".join(chr(ord("a") + int(ch, 16)) for ch in digest)


# NOTE: Chrome 138+ gates chrome.userScripts behind a per-extension
# "Allow user scripts" toggle stored in the MAC-signed `Secure Preferences`.
# A plain `Preferences` write does NOT enable it; the toggle must be flipped
# via the chrome://extensions UI so Chrome writes a valid HMAC itself.


# ---------------------------------------------------------------------------
#  Chrome launch / kill
# ---------------------------------------------------------------------------

@dataclass
class ChromeHandle:
    ws_url: str
    profile_path: Path
    pid: int
    port: int


def kill_chrome(pid: int) -> None:
    try:
        os.kill(pid, signal.SIGTERM)
        for _ in range(25):  # up to 5 s
            time.sleep(0.2)
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                break
        else:
            os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


def launch_cft_with_extension(
    cft_binary: Path,
    ext_dir: Path,
    *,
    rdp_port: int = 0,
) -> ChromeHandle:
    """Launch Chrome for Testing with --load-extension, wait for CDP ready.

    Detects the CDP websocket URL from either the DevToolsActivePort file
    (port=0) or stderr (Chrome 148+ with explicit port skips the file).
    """
    profile_dir = Path(tempfile.mkdtemp(prefix="bd-e2e-chrome-"))
    stderr_path = profile_dir / "_cft_stderr.log"
    stderr_fh = open(stderr_path, "w")
    args = [
        str(cft_binary),
        f"--user-data-dir={profile_dir}",
        f"--remote-debugging-port={rdp_port}",
        "--no-first-run",
        "--no-default-browser-check",
        "--remote-allow-origins=*",
        "--no-proxy-server",
        "--enable-features=UserScriptUserExtensionToggle",
        f"--load-extension={ext_dir}",
        "about:blank",
    ]
    proc = subprocess.Popen(
        args,
        stdout=subprocess.DEVNULL,
        stderr=stderr_fh,
        start_new_session=True,
    )
    try:
        active_file = profile_dir / "DevToolsActivePort"
        deadline = time.monotonic() + 15.0
        while time.monotonic() < deadline:
            # Method 1: DevToolsActivePort file (Chrome with port=0)
            try:
                lines = active_file.read_text().splitlines()
                if len(lines) >= 2 and lines[0].strip() and lines[1].strip():
                    port = int(lines[0].strip())
                    ws_path = lines[1].strip()
                    ws_url = f"ws://127.0.0.1:{port}{ws_path}"
                    stderr_fh.close()
                    return ChromeHandle(
                        ws_url=ws_url,
                        profile_path=profile_dir,
                        pid=proc.pid,
                        port=port,
                    )
            except (FileNotFoundError, OSError, ValueError):
                pass
            # Method 2: parse stderr for "DevTools listening on ws://..."
            # Chrome 148+ with explicit port writes to stderr but not the file.
            try:
                stderr_fh.flush()
                text = stderr_path.read_text()
                import re as _re
                m = _re.search(
                    r"DevTools listening on (ws://127\.0\.0\.1:(\d+)/\S+)", text
                )
                if m:
                    ws_url = m.group(1)
                    port = int(m.group(2))
                    stderr_fh.close()
                    return ChromeHandle(
                        ws_url=ws_url,
                        profile_path=profile_dir,
                        pid=proc.pid,
                        port=port,
                    )
            except (FileNotFoundError, OSError):
                pass
            if proc.poll() is not None:
                raise RuntimeError(
                    f"Chrome for Testing exited with code {proc.returncode}"
                )
            time.sleep(0.2)
        raise RuntimeError(
            f"Chrome for Testing did not become CDP-ready within 15s; "
            f"profile={profile_dir}"
        )
    except BaseException:
        stderr_fh.close()
        kill_chrome(proc.pid)
        shutil.rmtree(profile_dir, ignore_errors=True)
        raise


# ---------------------------------------------------------------------------
#  Daemon spawn + status polling
# ---------------------------------------------------------------------------

@dataclass
class DaemonHandle:
    proc: subprocess.Popen
    ext_port: int
    runtime_dir: str   # XDG_RUNTIME_DIR the daemon's fixed socket lives under
    log_path: Path


def scrubbed_env() -> dict[str, str]:
    """Return os.environ with BD_*/BS_*/BU_* vars stripped."""
    return {
        k: v for k, v in os.environ.items()
        if not k.startswith(("BD_", "BS_", "BU_"))
    }


def port_free(port: int) -> bool:
    with closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as s:
        try:
            s.bind(("127.0.0.1", port))
            return True
        except OSError:
            return False


def spawn_daemon(
    ext_port: int,
    runtime_dir: str,
    log_path: Path,
    *,
    env: dict[str, str] | None = None,
) -> DaemonHandle:
    """Spawn ``browserwright-daemon serve`` and wait until /__status__ responds.

    Single-global-daemon: isolation is via ``runtime_dir`` (XDG_RUNTIME_DIR →
    distinct fixed socket) + the relay port, NOT a ``--name``."""
    if not port_free(ext_port):
        raise RuntimeError(
            f"port {ext_port} already in use; another daemon running? "
            f"lsof -i :{ext_port}"
        )

    run_env = env or scrubbed_env()
    run_env["XDG_RUNTIME_DIR"] = runtime_dir
    run_env["TMPDIR"] = runtime_dir
    run_env["BD_EXTENSION_PORT"] = str(ext_port)
    run_env["BD_CONFIG"] = ""

    log_fh = open(log_path, "wb")
    proc = subprocess.Popen(
        [
            sys.executable, "-m", "browserwright.daemon.cli",
            "serve",
            "--backend", "extension",
            "--extension-port", str(ext_port),
            "-v",
        ],
        stdout=log_fh,
        stderr=subprocess.STDOUT,
        env=run_env,
    )

    try:
        poll_status(ext_port, timeout=10.0, proc=proc, log_path=log_path)
    except Exception:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=2)
        log_fh.close()
        raise

    return DaemonHandle(proc=proc, ext_port=ext_port,
                        runtime_dir=runtime_dir, log_path=log_path)


def poll_status(
    port: int,
    *,
    timeout: float = 10.0,
    proc: subprocess.Popen | None = None,
    log_path: Path | None = None,
    require_extensions: int = 0,
) -> dict:
    """Poll /__status__ until it responds (and optionally extensions >= N)."""
    deadline = time.monotonic() + timeout
    last_err: Exception | None = None
    while time.monotonic() < deadline:
        if proc is not None and proc.poll() is not None:
            raise RuntimeError(
                f"daemon exited early (code {proc.returncode}); "
                f"see {log_path}"
            )
        try:
            with urllib.request.urlopen(
                f"http://127.0.0.1:{port}/__status__", timeout=0.5
            ) as resp:
                body = json.loads(resp.read().decode("utf-8"))
            if int(body.get("extensions", 0)) >= require_extensions:
                return body
        except (urllib.error.URLError, OSError, json.JSONDecodeError) as e:
            last_err = e
        time.sleep(0.2)
    raise RuntimeError(
        f"/__status__ not ready within {timeout}s (need extensions>={require_extensions}); "
        f"last err={last_err}; log={log_path}"
    )


def stop_daemon(handle: DaemonHandle) -> None:
    """Terminate the daemon process."""
    handle.proc.terminate()
    try:
        handle.proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        handle.proc.kill()
        handle.proc.wait(timeout=2)
