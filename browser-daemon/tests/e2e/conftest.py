"""pytest configuration for real-Chrome E2E tests.

These tests:
- launch a real Chrome with the patched extension (port 29989)
- spawn a real `browser-daemon serve`
- drive everything through the `browser-skill` CLI

They are SKIPPED unless explicitly selected, either by path
(`pytest tests/e2e/`) or by marker (`pytest -m real_chrome`).
The patcher unit test (test_patch_extension.py) does NOT carry the marker
so it remains discoverable in the inner loop.
"""
from __future__ import annotations

import hashlib
import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path

import pytest

# Test-only ports. Chosen to be distinct from production (19989) and far enough
# from common dev ports to reduce collisions.
TEST_EXT_PORT = 29989
TEST_RDP_PORT = 29990
TEST_NAME = "bd-e2e"


def scrubbed_env() -> dict[str, str]:
    """Return os.environ with BD_*/BS_*/BU_* vars stripped.

    Prevents the user's shell environment from leaking into test
    subprocesses (e.g. BD_RDP_PORT, BD_BACKEND, BS_DAEMON_URL_CMD).
    Callers re-add only the vars they need for isolation.
    """
    return {k: v for k, v in os.environ.items()
            if not k.startswith(("BD_", "BS_", "BU_"))}


def pytest_collection_modifyitems(config, items):
    """Auto-mark every test in tests/e2e/ (except _patch_extension test) as
    `real_chrome`, and skip them unless the user asked for them.

    Selection rule:
        - User passed `-m real_chrome` (or any expression that matches)  -> run
        - User passed an explicit path under tests/e2e/ matching the test -> run
        - Else -> skip with a clear reason.
    """
    rootdir = config.rootpath
    # 1. Tag everything in tests/e2e/ (except the patcher unit test) with
    #    `real_chrome`.
    for item in items:
        try:
            rel = item.path.relative_to(rootdir)
        except ValueError:
            continue
        parts = rel.parts
        if len(parts) >= 2 and parts[0] == "tests" and parts[1] == "e2e":
            if item.path.name == "test_patch_extension.py":
                continue
            item.add_marker(pytest.mark.real_chrome)

    # 2. If the user did NOT explicitly opt-in, skip real_chrome tests.
    if _opted_in_to_real_chrome(config):
        return
    skip = pytest.mark.skip(
        reason="real_chrome E2E -- opt in with `pytest tests/e2e/` "
               "or `pytest -m real_chrome`"
    )
    for item in items:
        if "real_chrome" in item.keywords:
            item.add_marker(skip)


def _opted_in_to_real_chrome(config) -> bool:
    # Marker expression mentions real_chrome.
    mark_expr = config.getoption("-m", default="") or ""
    if "real_chrome" in mark_expr:
        return True
    # Any positional arg points under tests/e2e/.
    for arg in config.args:
        if "tests/e2e" in arg.replace("\\", "/"):
            return True
    return False


# ---------------------------------------------------------------------------
#   Fixtures
# ---------------------------------------------------------------------------


@dataclass
class DaemonHandle:
    proc: subprocess.Popen
    ext_port: int
    name: str
    log_path: Path


def _port_free(port: int) -> bool:
    with closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as s:
        try:
            s.bind(("127.0.0.1", port))
            return True
        except OSError:
            return False


@pytest.fixture(scope="session")
def e2e_artifacts_dir() -> Path:
    d = Path(__file__).parent / "_artifacts"
    d.mkdir(parents=True, exist_ok=True)
    return d


@pytest.fixture(scope="session")
def e2e_daemon(e2e_artifacts_dir, tmp_path_factory):
    """Spawn `browser-daemon serve --backend extension --extension-port N
    --name bd-e2e` for the duration of the session. Yields a DaemonHandle.
    """
    if not _port_free(TEST_EXT_PORT):
        pytest.fail(
            f"port {TEST_EXT_PORT} already in use; another test daemon? "
            "Use `lsof -i :29989` to find it."
        )

    log_path = e2e_artifacts_dir / "daemon.log"
    log_fh = open(log_path, "wb")  # noqa: SIM115 — closed in teardown

    env = os.environ.copy()
    env["BD_NAME"] = TEST_NAME
    # Neutralise any externally-set BD_CONFIG so the test daemon doesn't
    # inherit the user's toml (which may set relay_url, ports, etc.).
    # Empty string means "no config file" — the daemon falls through to
    # defaults, which are then overridden by our explicit CLI flags above.
    env["BD_CONFIG"] = ""

    proc = subprocess.Popen(
        [
            sys.executable, "-m", "browser_daemon.cli",
            "serve",
            "--backend", "extension",
            "--extension-port", str(TEST_EXT_PORT),
            "--name", TEST_NAME,
            "-v",
        ],
        stdout=log_fh,
        stderr=subprocess.STDOUT,
        env=env,
    )

    # Wait until /__status__ responds.
    deadline = time.monotonic() + 10.0
    last_err: Exception | None = None
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            log_fh.flush()
            pytest.fail(
                f"daemon exited early with code {proc.returncode}; "
                f"see {log_path}"
            )
        try:
            with urllib.request.urlopen(
                f"http://127.0.0.1:{TEST_EXT_PORT}/__status__", timeout=0.5
            ) as resp:
                if resp.status == 200:
                    break
        except (urllib.error.URLError, OSError) as e:
            last_err = e
            time.sleep(0.2)
    else:
        log_fh.flush()
        pytest.fail(
            f"daemon /__status__ never came up within 10s; last err={last_err}; "
            f"see {log_path}"
        )

    yield DaemonHandle(proc=proc, ext_port=TEST_EXT_PORT, name=TEST_NAME, log_path=log_path)

    # Teardown.
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=2)
    log_fh.close()


# ---------------------------------------------------------------------------
#   Chrome + extension fixtures
# ---------------------------------------------------------------------------

import asyncio
import glob as _glob
import platform
import shutil
import signal
import tempfile
import uuid

from browser_daemon import launch_chrome as _lc_mod
from browser_daemon.config import load as _load_config

EXT_SOURCE_DIR = Path(__file__).resolve().parents[2] / "chrome-extension"

# Chrome stable (Google-branded) blocks --load-extension. We need Chrome for
# Testing (CfT), which is Chromium-based and allows it. CfT is installed via:
#   npx @puppeteer/browsers install chrome@stable --path <dest>
# The fixture discovers CfT at standard locations or skips with a message.
_CFT_SEARCH_DIRS = [
    Path("/tmp/chrome-for-testing"),
    Path.home() / ".cache" / "puppeteer",
    Path.home() / ".cache" / "chrome-for-testing",
]


def _find_cft_binary() -> Path | None:
    """Discover Chrome for Testing binary. Returns None if not found."""
    system = platform.system()
    if system == "Darwin":
        patterns = [
            "chrome/*/chrome-*/Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing",
        ]
    elif system == "Linux":
        patterns = ["chrome/*/chrome-*/chrome"]
    else:
        return None  # Windows: TODO

    for base in _CFT_SEARCH_DIRS:
        if not base.is_dir():
            continue
        for pat in patterns:
            matches = sorted(base.glob(pat))
            if matches:
                return matches[-1]  # newest version
    return None


@pytest.fixture(scope="session")
def patched_ext_dir():
    from tests.e2e._patch_extension import patch_extension_dir
    d = patch_extension_dir(EXT_SOURCE_DIR, relay_port=TEST_EXT_PORT)
    yield d
    shutil.rmtree(d, ignore_errors=True)


def _extension_id_from_path(ext_dir: Path) -> str:
    digest = hashlib.sha256(str(ext_dir.resolve()).encode("utf-8")).hexdigest()[:32]
    return "".join(chr(ord("a") + int(ch, 16)) for ch in digest)


# NOTE: Chrome 138+ gates chrome.userScripts behind a per-extension
# "Allow user scripts" toggle whose state lives in the MAC-signed
# `Secure Preferences` file. Writing a plain `Preferences` entry does NOT
# enable it (Chrome ignores/rebuilds it). The toggle must be flipped through
# the chrome://extensions UI so Chrome writes a valid HMAC itself — see
# `_enable_user_scripts_toggle` in test_userscripts_e2e.py.


@dataclass
class ChromeHandle:
    ws_url: str
    profile_path: Path
    pid: int
    port: int


def _kill_chrome(pid: int):
    try:
        os.kill(pid, signal.SIGTERM)
        for _ in range(25):  # up to 5s
            time.sleep(0.2)
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                break
        else:
            os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


def _launch_cft_with_extension(
    cft_binary: Path, ext_dir: Path, *, rdp_port: int = 0,
) -> ChromeHandle:
    """Launch Chrome for Testing with --load-extension, wait for CDP ready."""
    profile_dir = Path(tempfile.mkdtemp(prefix="bd-e2e-chrome-"))
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
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )

    # Wait for DevToolsActivePort. On ANY failure path, kill Chrome and
    # remove the profile dir so we don't leak processes or tmpdir space.
    try:
        active_file = profile_dir / "DevToolsActivePort"
        deadline = time.monotonic() + 15.0
        while time.monotonic() < deadline:
            try:
                lines = active_file.read_text().splitlines()
                if len(lines) >= 2 and lines[0].strip() and lines[1].strip():
                    port = int(lines[0].strip())
                    ws_path = lines[1].strip()
                    ws_url = f"ws://127.0.0.1:{port}{ws_path}"
                    return ChromeHandle(
                        ws_url=ws_url,
                        profile_path=profile_dir,
                        pid=proc.pid,
                        port=port,
                    )
            except (FileNotFoundError, OSError, ValueError):
                pass
            if proc.poll() is not None:
                raise RuntimeError(
                    f"Chrome for Testing exited with code {proc.returncode}"
                )
            time.sleep(0.2)

        raise RuntimeError(
            f"Chrome for Testing did not write DevToolsActivePort within 15s; "
            f"profile={profile_dir}"
        )
    except BaseException:
        _kill_chrome(proc.pid)
        shutil.rmtree(profile_dir, ignore_errors=True)
        raise


@pytest.fixture(scope="session")
def cft_binary():
    """Discover Chrome for Testing binary, skip if not found."""
    binary = _find_cft_binary()
    if binary is None:
        pytest.skip(
            "Chrome for Testing not found. Install via: "
            "npx @puppeteer/browsers install chrome@stable "
            "--path /tmp/chrome-for-testing"
        )
    return binary


@pytest.fixture
def e2e_chrome(cft_binary, patched_ext_dir):
    """Launch Chrome for Testing with the patched extension loaded.

    Function-scoped: every test gets a clean Chrome. Uses Chrome for Testing
    because Google Chrome stable blocks --load-extension (Chrome 148+).
    """
    handle = _launch_cft_with_extension(cft_binary, patched_ext_dir)
    yield handle
    _kill_chrome(handle.pid)
    shutil.rmtree(handle.profile_path, ignore_errors=True)


# ---------------------------------------------------------------------------
#   Artifact dump on failure
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _e2e_dump_artifacts_on_failure(request, e2e_artifacts_dir):
    """When a `real_chrome` test fails, write env into `_artifacts/<nodeid>/`."""
    yield
    rep = getattr(request.node, "rep_call", None)
    if rep is not None and rep.failed:
        outdir = e2e_artifacts_dir / request.node.name
        outdir.mkdir(parents=True, exist_ok=True)
        env_lines = [f"{k}={v}" for k, v in sorted(os.environ.items())
                     if k.startswith(("BD_", "BS_", "BU_"))]
        (outdir / "env.txt").write_text("\n".join(env_lines), encoding="utf-8")


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    outcome = yield
    rep = outcome.get_result()
    setattr(item, f"rep_{rep.when}", rep)


@pytest.fixture
def ext_ready(e2e_daemon, e2e_chrome):
    """Block until the extension SW has connected to the daemon's relay.

    Polls `/__status__` and asserts `extensions >= 1` within 10s.
    On timeout, fails the test with the daemon log location.
    """
    deadline = time.monotonic() + 10.0
    last_status: dict | None = None
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(
                f"http://127.0.0.1:{e2e_daemon.ext_port}/__status__", timeout=0.5
            ) as resp:
                body = json.loads(resp.read().decode("utf-8"))
            last_status = body
            if int(body.get("extensions", 0)) >= 1:
                return body
        except (urllib.error.URLError, OSError, json.JSONDecodeError):
            pass
        time.sleep(0.2)
    pytest.fail(
        f"extension never connected within 10s; last status={last_status}; "
        f"daemon log: {e2e_daemon.log_path}"
    )


@pytest.fixture
def e2e_chrome_rdp(tmp_path_factory):
    """Chrome with --remote-debugging-port for RDP-backend tests.
    No extension — RDP backend doesn't need one. Uses regular Chrome.
    """
    cfg = _load_config(env={})
    profile_name = f"bd-e2e-rdp-{uuid.uuid4().hex[:8]}"
    out = asyncio.run(_lc_mod.launch_chrome(
        cfg,
        profile=profile_name,
        persistent=False,
        port=TEST_RDP_PORT,
    ))
    handle = ChromeHandle(
        ws_url=out["ws_url"],
        profile_path=Path(out["extras"]["profile_path"]),
        pid=int(out["extras"]["pid"]),
        port=TEST_RDP_PORT,
    )
    yield handle
    _kill_chrome(handle.pid)
    shutil.rmtree(handle.profile_path, ignore_errors=True)
