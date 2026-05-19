# Real-Extension E2E Harness Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Build a pytest-based E2E harness that exercises a real Chrome + the locally-built extension + a real daemon, driven through the `browser-skill` CLI. Lets agents (and humans) verify extension/daemon changes without touching the user's daily Chrome.

**Architecture:** Isolation by env-var only (no production code changes besides one parameter on `launch_chrome`). Test daemon binds extension port `29989`, runs under `BD_NAME=bd-e2e`. Chrome runs against a tmpdir profile with a sed-patched copy of the extension whose `RELAY_URL` points at `29989` instead of `19989`. Skill points at the test daemon via `BS_DAEMON_URL_CMD` env var. Tests live under `browser-daemon/tests/e2e/` and only run when explicitly invoked.

**Tech Stack:**
- Python 3.11, pytest 8 (already in `browser-daemon[test]`)
- `subprocess` for daemon/Chrome/skill control
- `httpx` for `/__status__` polling (already a dep)
- Existing `browser_daemon.launch_chrome.launch_chrome()` Python API for Chrome lifecycle

**Source design:** `docs/plans/2026-05-19-real-extension-e2e-design.md` — all §1-4 decisions binding.

**Reference codebase facts** (verified during plan writing):
- Daemon CLI binary: `browser-daemon` (`browser-daemon/pyproject.toml` script).
- Skill CLI binary: `browser-skill` (`browser-skill/pyproject.toml` script).
- Daemon `serve --backend extension --extension-port N --name X`: already supported.
- Daemon `url --backend rdp --port N`: already supported (resolves ws via `/json/version`).
- Skill env hooks (verified in `browser-skill/src/browser_skill/daemon_client.py`):
  - `BS_DAEMON_URL_CMD` — override the URL-resolution command (default `browser-daemon url`).
  - `BS_DAEMON_BACKEND` — appends `--backend X` to the URL command.
  - `BS_CDP_WS` — bypass daemon resolution entirely, use this ws URL.
- Extension `RELAY_URL` is hardcoded at `browser-daemon/chrome-extension/background.js:39` as `"ws://127.0.0.1:19989/"`.
- Extension `manifest.json` `host_permissions` already covers `ws://127.0.0.1/*` (any port works).
- `launch_chrome.launch_chrome()` signature is at `browser-daemon/src/browser_daemon/launch_chrome.py` ~line 32-44; the args list it builds is at lines 86-102.

---

## Phase 1 — Foundation: `launch_chrome` extension hook

Sole production-code change in this plan. Everything downstream depends on this.

### Task 1: Add `extra_args` parameter to `launch_chrome.launch_chrome()`

**Why:** E2E fixtures need to inject `--load-extension=<patched-dir>` when launching the test Chrome. We add a generic `extra_args: list[str] | None = None` rather than a special-case `--load-extension` flag — keeps `launch_chrome` general.

**Files:**
- Modify: `browser-daemon/src/browser_daemon/launch_chrome.py` (signature ~line 32-44; args list ~line 86-102)
- Modify (test): `browser-daemon/tests/test_launch_chrome.py`

**Step 1.1: Write the failing test**

Append to `browser-daemon/tests/test_launch_chrome.py`:

```python
@pytest.mark.asyncio
async def test_launch_chrome_passes_extra_args(monkeypatch, tmp_path, fake_chrome):
    """extra_args list is appended verbatim to the Chrome argv."""
    cfg = Config()
    captured: list[list[str]] = []

    real_popen = lc_mod.subprocess.Popen

    def fake_popen(args, **kw):
        captured.append(list(args))
        return real_popen(args, **kw)

    monkeypatch.setattr(lc_mod.subprocess, "Popen", fake_popen)

    out = await lc_mod.launch_chrome(
        cfg,
        profile="isolated",
        chrome_binary=str(fake_chrome),
        port=0,
        extra_args=["--load-extension=/tmp/fake-ext", "--disable-features=Foo"],
    )
    assert out["schema_version"] == 1
    assert captured, "Popen never called"
    argv = captured[0]
    assert "--load-extension=/tmp/fake-ext" in argv
    assert "--disable-features=Foo" in argv
    # extra_args appended after the existing flags, not interleaved before them
    assert argv.index("--remote-allow-origins=*") < argv.index("--load-extension=/tmp/fake-ext")
```

Match the import / fixture style already used in this file (look at the existing `test_launch_chrome_end_to_end` ~line 56 to mirror the `fake_chrome` fixture usage).

**Step 1.2: Run test and verify it fails**

```bash
cd browser-daemon && uv run pytest tests/test_launch_chrome.py::test_launch_chrome_passes_extra_args -v
```
Expected: `FAILED` with `TypeError: launch_chrome() got an unexpected keyword argument 'extra_args'`.

**Step 1.3: Add the parameter and thread it into the args list**

In `launch_chrome.py`, function signature: add after `allow_default_profile: bool = False`:

```python
    extra_args: list[str] | None = None,
```

Then in the `args` list (currently ending with `"--remote-allow-origins=*"`), after that line, before `]`:

```python
    if extra_args:
        args.extend(extra_args)
```

Update the docstring with one line under "Returns…":

```
`extra_args` (optional list) is appended to the Chrome argv verbatim, after
the framework's own flags. Used by the E2E harness to inject
`--load-extension=...`. Caller is responsible for shell-escaping.
```

**Step 1.4: Run test, verify it passes**

```bash
cd browser-daemon && uv run pytest tests/test_launch_chrome.py -v
```
Expected: all tests pass, including the new one. Existing tests must not regress.

**Step 1.5: Commit**

```bash
git add browser-daemon/src/browser_daemon/launch_chrome.py browser-daemon/tests/test_launch_chrome.py
git commit -m "feat(launch_chrome): accept extra_args for --load-extension etc."
```

### ⏸ Review checkpoint after Phase 1

Verify before continuing:
- `uv run pytest browser-daemon/tests/` (full suite) — green.
- The diff to `launch_chrome.py` is **only** the new parameter + the appending block; no other behaviour changed.
- New test exercises both ordering (after framework flags) and presence.

---

## Phase 2 — Test infrastructure (no assertions yet)

Build the fixtures + helpers. Each task ends with a "fixtures import cleanly" check; assertions land in Phase 3.

### Task 2: Extension patcher helper

**Why:** Need a clean, hermetic copy of `chrome-extension/` with `RELAY_URL` rewritten to point at the test port. Pure file-system work, easy to unit-test.

**Files:**
- Create: `browser-daemon/tests/e2e/__init__.py` (empty)
- Create: `browser-daemon/tests/e2e/_patch_extension.py`
- Create: `browser-daemon/tests/e2e/test_patch_extension.py`

**Step 2.1: Write `_patch_extension.py`**

```python
"""Copy chrome-extension/ to a tmpdir and rewrite RELAY_URL to the test port.

Used only by the e2e fixtures — never imported by production code.
"""
from __future__ import annotations

import re
import shutil
import tempfile
from pathlib import Path

# `const RELAY_URL = "ws://127.0.0.1:19989/";`  (background.js)
RELAY_URL_RE = re.compile(r'(const\s+RELAY_URL\s*=\s*")ws://127\.0\.0\.1:\d+(/?")')


def patch_extension_dir(src_dir: Path, *, relay_port: int) -> Path:
    """Copy `src_dir` to a fresh tmpdir and rewrite RELAY_URL in background.js.

    Returns the path to the patched copy. Caller is responsible for cleanup
    (use `tempfile.mkdtemp` + rmtree in the fixture teardown).
    """
    if not src_dir.is_dir():
        raise FileNotFoundError(f"extension source not a directory: {src_dir}")
    dst = Path(tempfile.mkdtemp(prefix="bd-e2e-ext-"))
    # Copy contents into dst (not into a sub-dir) so --load-extension=dst works.
    for child in src_dir.iterdir():
        target = dst / child.name
        if child.is_dir():
            shutil.copytree(child, target)
        else:
            shutil.copy2(child, target)

    bg = dst / "background.js"
    text = bg.read_text(encoding="utf-8")
    new_text, n = RELAY_URL_RE.subn(rf'\g<1>ws://127.0.0.1:{relay_port}\g<2>', text)
    if n != 1:
        raise RuntimeError(
            f"expected exactly one RELAY_URL constant in {bg}, found {n}"
        )
    bg.write_text(new_text, encoding="utf-8")
    return dst
```

**Step 2.2: Write its test**

```python
"""Unit tests for the extension patcher (no Chrome involved)."""
from __future__ import annotations

from pathlib import Path

import pytest

from tests.e2e._patch_extension import patch_extension_dir

EXT_SRC = Path(__file__).resolve().parents[2] / "chrome-extension"


def test_patch_extension_rewrites_relay_url(tmp_path):
    out = patch_extension_dir(EXT_SRC, relay_port=29989)
    bg = (out / "background.js").read_text(encoding="utf-8")
    assert 'ws://127.0.0.1:29989/' in bg
    assert 'ws://127.0.0.1:19989/' not in bg
    # manifest.json must be present and untouched
    assert (out / "manifest.json").is_file()


def test_patch_extension_missing_source_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        patch_extension_dir(tmp_path / "does-not-exist", relay_port=29989)
```

**Step 2.3: Run, verify pass**

```bash
cd browser-daemon && uv run pytest tests/e2e/test_patch_extension.py -v
```
Expected: 2 passed.

**Step 2.4: Commit**

```bash
git add browser-daemon/tests/e2e/
git commit -m "test(e2e): add extension patcher + unit test"
```

### Task 3: pytest config — register `real_chrome` marker, gitignore artifacts

**Why:** E2E tests need to be *opt-in* (don't run on default `pytest`). Marker + `conftest` machinery achieves that. Artifacts directory must not be committed.

**Files:**
- Modify: `browser-daemon/pyproject.toml` (`[tool.pytest.ini_options]` section)
- Modify: `.gitignore` (repo root)
- Create: `browser-daemon/tests/e2e/conftest.py`

**Step 3.1: Add marker to pyproject**

In `browser-daemon/pyproject.toml`, under `[tool.pytest.ini_options]`, add:

```toml
markers = [
    "real_chrome: end-to-end test that launches a real Chrome + extension (skipped unless explicitly selected)",
]
```

**Step 3.2: Add `.gitignore` entry**

Append to `browser-daemon/.gitignore` (create if absent — check first; repo root `.gitignore` is at `/Users/metajs/gitRepos/labs/browser/.gitignore`):

```
browser-daemon/tests/e2e/_artifacts/
```

(Put in the repo-root `.gitignore` since that's where the existing `.gitignore` lives — verify by `git check-ignore` after.)

**Step 3.3: Create `tests/e2e/conftest.py`**

```python
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

import pytest


def pytest_collection_modifyitems(config, items):
    """Auto-mark every test in tests/e2e/ (except _patch_extension test) as
    `real_chrome`, and skip them unless the user asked for them.

    Selection rule:
        - User passed `-m real_chrome` (or any expression that matches)  → run
        - User passed an explicit path under tests/e2e/ matching the test → run
        - Else → skip with a clear reason.
    """
    rootdir = config.rootpath
    # 1. Tag everything in tests/e2e/ (except the patcher unit test) with
    #    `real_chrome` and `_real_chrome_dir = True` so the skip logic below
    #    can distinguish "in e2e dir" from "carries marker".
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
        reason="real_chrome E2E — opt in with `pytest tests/e2e/` "
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
```

**Step 3.4: Verify inner-loop pytest is unaffected**

```bash
cd browser-daemon && uv run pytest tests/ -v
```
Expected: existing tests still pass. The new `tests/e2e/test_patch_extension.py` runs (it does NOT get the auto-skip — it has no `real_chrome` marker after Step 3.3 special-cases it). Any other test in tests/e2e/ added later auto-marks `real_chrome` and gets auto-skipped.

Also verify the opt-in path:

```bash
cd browser-daemon && uv run pytest tests/e2e/ -v
```
Expected: only `test_patch_extension.py` collected runs and passes; no real_chrome bodies yet (we haven't written them).

**Step 3.5: Commit**

```bash
git add browser-daemon/pyproject.toml browser-daemon/tests/e2e/conftest.py .gitignore
git commit -m "test(e2e): register real_chrome marker + opt-in collection"
```

### Task 4: `e2e_daemon` fixture (session-scoped)

**Why:** Spawns the test daemon and confirms `/__status__` is reachable. All later fixtures depend on this.

**Files:**
- Modify: `browser-daemon/tests/e2e/conftest.py`
- Create: `browser-daemon/tests/e2e/test_l0_smoke.py` (just placeholder so the fixture has a consumer to verify)

**Step 4.1: Add the fixture to `conftest.py`**

Append:

```python
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

# Test-only ports. Chosen to be distinct from production (19989) and far enough
# from common dev ports to reduce collisions. If 29989/29990 are in use the
# fixture fails loudly — don't paper over it with port-picking.
TEST_EXT_PORT = 29989
TEST_RDP_PORT = 29990
TEST_NAME = "bd-e2e"


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
    # Force config to a tmp path so we don't write to ~/.config/browser-daemon
    env["BS_DAEMON_CONFIG_PATH"] = str(tmp_path_factory.mktemp("bd-cfg"))

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
```

**Step 4.2: Add a smoke test that just uses the fixture**

Create `tests/e2e/test_l0_smoke.py`:

```python
"""L0 smoke — daemon and Chrome reachability."""
from __future__ import annotations

import urllib.request


def test_e2e_daemon_status_ok(e2e_daemon):
    """Daemon `/__status__` returns 200; no extension yet, so
    extensions_connected should be 0."""
    with urllib.request.urlopen(
        f"http://127.0.0.1:{e2e_daemon.ext_port}/__status__", timeout=2
    ) as resp:
        assert resp.status == 200
```

**Step 4.3: Run, verify pass**

```bash
cd browser-daemon && uv run pytest tests/e2e/test_l0_smoke.py::test_e2e_daemon_status_ok -v
```
Expected: 1 passed. Check `tests/e2e/_artifacts/daemon.log` exists and contains daemon startup chatter.

**Step 4.4: Commit**

```bash
git add browser-daemon/tests/e2e/
git commit -m "test(e2e): add session-scoped e2e_daemon fixture + smoke"
```

### Task 5: `patched_ext_dir` + `e2e_chrome` + `ext_ready` fixtures

**Why:** These three compose: Chrome can't start without the patched extension; the test isn't ready to assert until the extension SW has dialed in.

**Files:**
- Modify: `browser-daemon/tests/e2e/conftest.py`

**Step 5.1: Add patched_ext_dir fixture**

```python
import shutil

from browser_daemon import launch_chrome as _lc_mod
from browser_daemon.config import Config


EXT_SOURCE_DIR = Path(__file__).resolve().parents[2] / "chrome-extension"


@pytest.fixture(scope="session")
def patched_ext_dir():
    from tests.e2e._patch_extension import patch_extension_dir
    d = patch_extension_dir(EXT_SOURCE_DIR, relay_port=TEST_EXT_PORT)
    yield d
    shutil.rmtree(d, ignore_errors=True)
```

**Step 5.2: Add e2e_chrome fixture (function-scoped)**

```python
import asyncio
import uuid


@dataclass
class ChromeHandle:
    ws_url: str
    profile_path: Path
    pid: int


@pytest.fixture
def e2e_chrome(patched_ext_dir, e2e_artifacts_dir, tmp_path_factory):
    """Launch a fresh isolated-profile Chrome with the patched extension loaded.

    Function-scoped: every test gets a clean Chrome. The Chrome process is
    killed in teardown; the tmp profile dir is removed.
    """
    cfg = Config()
    profile_name = f"bd-e2e-{uuid.uuid4().hex[:8]}"
    # We override the user-data-dir by overriding `_allocate_data_dir` indirectly:
    # the public API uses `profile=<name>` + `persistent=True/False`. Use tmp=True
    # equivalent via `persistent=False` — that allocates a fresh tmpdir each call.
    # Verify by inspecting launch_chrome._allocate_data_dir if behaviour diverges.

    out = asyncio.run(_lc_mod.launch_chrome(
        cfg,
        profile=profile_name,
        persistent=False,
        extra_args=[f"--load-extension={patched_ext_dir}"],
    ))
    handle = ChromeHandle(
        ws_url=out["ws_url"],
        profile_path=Path(out["extras"]["profile_path"]),
        pid=int(out["extras"]["pid"]),
    )

    yield handle

    # Teardown: terminate Chrome, remove profile dir.
    import signal
    try:
        os.kill(handle.pid, signal.SIGTERM)
        for _ in range(25):  # up to 5s
            time.sleep(0.2)
            try:
                os.kill(handle.pid, 0)
            except ProcessLookupError:
                break
        else:
            os.kill(handle.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    shutil.rmtree(handle.profile_path, ignore_errors=True)
```

> **Note for the implementer:** if `persistent=False` does not produce a fresh tmpdir (the API has shifted between versions), read `launch_chrome.py:_allocate_data_dir` and adjust — possibly by setting an env var or passing a `--tmp` equivalent. The contract this fixture needs is "a *fresh* user-data-dir per launch."

**Step 5.3: Add ext_ready fixture**

```python
import json


@pytest.fixture
def ext_ready(e2e_daemon, e2e_chrome):
    """Block until the extension SW has connected to the daemon's relay.

    Polls `/__status__` and asserts `extensions_connected >= 1` within 10s.
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
            if int(body.get("extensions_connected", 0)) >= 1:
                return body
        except (urllib.error.URLError, OSError, json.JSONDecodeError):
            pass
        time.sleep(0.2)
    pytest.fail(
        f"extension never connected within 10s; last status={last_status}; "
        f"daemon log: {e2e_daemon.log_path}"
    )
```

**Step 5.4: Add a smoke test that exercises all three**

Append to `tests/e2e/test_l0_smoke.py`:

```python
def test_extension_connects_to_daemon(ext_ready):
    """L0 extension backend: a real Chrome with the patched extension loaded
    is able to dial the test daemon's relay."""
    assert ext_ready["extensions_connected"] >= 1
```

**Step 5.5: Run, verify pass**

```bash
cd browser-daemon && uv run pytest tests/e2e/test_l0_smoke.py -v
```
Expected: 2 passed. This is the first run that actually opens a Chrome window — confirm one appears (briefly) on macOS, then closes.

**Step 5.6: Commit**

```bash
git add browser-daemon/tests/e2e/
git commit -m "test(e2e): launch real Chrome + verify extension dials daemon"
```

### Task 6: `run_skill` helper

**Why:** All Phase 3 tests drive the daemon through the skill CLI. One helper avoids stamping out env-var boilerplate everywhere.

**Files:**
- Create: `browser-daemon/tests/e2e/helpers.py`

**Step 6.1: Write helpers.py**

```python
"""Helpers for running browser-skill against the test daemon."""
from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass

from .conftest import TEST_EXT_PORT, TEST_NAME, TEST_RDP_PORT


@dataclass
class SkillResult:
    returncode: int
    stdout: str
    stderr: str


def run_skill(script: str, *, backend: str, extra_env: dict[str, str] | None = None,
              timeout: float = 30.0) -> SkillResult:
    """Invoke `browser-skill` with the given heredoc-style Python script.

    Sets env vars so the skill resolves the *test* daemon, not the user's
    production daemon:

        - `BS_DAEMON_URL_CMD`  → invokes the test daemon's URL resolution
        - `BS_DAEMON_BACKEND`  → pins the backend
        - `BD_NAME`            → pins the daemon name

    Args:
        script: Python source the skill REPL will execute (heredoc body).
        backend: "extension" or "rdp".
        extra_env: extra env merged on top.
        timeout: subprocess timeout in seconds.

    Returns SkillResult (does NOT raise on non-zero exit; caller asserts).
    """
    if backend not in ("extension", "rdp"):
        raise ValueError(f"backend must be 'extension' or 'rdp', got {backend!r}")

    skill_bin = shutil.which("browser-skill")
    if not skill_bin:
        raise RuntimeError(
            "browser-skill not on PATH; install browser-skill in editable mode: "
            "`pip install -e browser-skill[test]`"
        )

    env = os.environ.copy()
    env["BD_NAME"] = TEST_NAME
    env["BS_DAEMON_BACKEND"] = backend
    if backend == "extension":
        env["BS_DAEMON_URL_CMD"] = (
            f"browser-daemon url --backend extension --name {TEST_NAME}"
        )
    else:  # rdp
        # daemon `url --backend rdp --port N` resolves directly via HTTP probe.
        env["BS_DAEMON_URL_CMD"] = (
            f"browser-daemon url --backend rdp --port {TEST_RDP_PORT}"
        )
    if extra_env:
        env.update(extra_env)

    proc = subprocess.run(
        [skill_bin],
        input=script,
        text=True,
        capture_output=True,
        env=env,
        timeout=timeout,
    )
    return SkillResult(
        returncode=proc.returncode,
        stdout=proc.stdout,
        stderr=proc.stderr,
    )
```

**Step 6.2: Verify import only — no test yet**

```bash
cd browser-daemon && uv run python -c "from tests.e2e.helpers import run_skill; print(run_skill)"
```
Expected: prints `<function run_skill at 0x...>` with no import error.

**Step 6.3: Commit**

```bash
git add browser-daemon/tests/e2e/helpers.py
git commit -m "test(e2e): add run_skill helper that points skill at test daemon"
```

### ⏸ Review checkpoint after Phase 2

Verify before continuing:
- `uv run pytest browser-daemon/tests/` — default-loop tests still green.
- `uv run pytest browser-daemon/tests/e2e/` — opens a Chrome, smoke + extension-connect both pass.
- All commits atomic, messages descriptive.
- `_artifacts/daemon.log` exists after an e2e run; `_artifacts/` is gitignored.
- No leftover Chrome processes after the run (`pgrep -fa bd-e2e` returns nothing).

---

## Phase 3 — Test bodies (L0 → L3)

Each task adds one file's worth of assertions.

### Task 7: L0 RDP smoke

We already have extension-backend L0 (Tasks 4-5). Add the RDP smoke now so L0 is complete.

**Files:**
- Modify: `browser-daemon/tests/e2e/conftest.py` (add `e2e_chrome_rdp` fixture)
- Modify: `browser-daemon/tests/e2e/test_l0_smoke.py`

**Step 7.1: Add RDP-only Chrome fixture (no extension, has remote-debugging-port)**

Append to `conftest.py`:

```python
@pytest.fixture
def e2e_chrome_rdp(tmp_path_factory):
    """Chrome with --remote-debugging-port for RDP-backend tests.
    No extension — RDP backend doesn't need one.
    """
    cfg = Config()
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
    )
    yield handle
    import signal
    try:
        os.kill(handle.pid, signal.SIGTERM)
        for _ in range(25):
            time.sleep(0.2)
            try:
                os.kill(handle.pid, 0)
            except ProcessLookupError:
                break
        else:
            os.kill(handle.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    shutil.rmtree(handle.profile_path, ignore_errors=True)
```

**Step 7.2: Add the RDP smoke test**

Append to `test_l0_smoke.py`:

```python
import subprocess


def test_rdp_backend_resolves_via_daemon(e2e_chrome_rdp):
    """L0 RDP backend: `browser-daemon url --backend rdp --port N` returns
    a non-empty ws URL when a Chrome is listening on that port."""
    proc = subprocess.run(
        ["browser-daemon", "url", "--backend", "rdp",
         "--port", str(e2e_chrome_rdp.ws_url.rsplit(':', 1)[-1].split('/')[0])],
        capture_output=True, text=True, timeout=10,
    )
    assert proc.returncode == 0, f"stderr: {proc.stderr}"
    url = proc.stdout.strip().splitlines()[0]
    assert url.startswith("ws://127.0.0.1:")
```

(The slicing on `ws_url` gets the port back out of `ws://127.0.0.1:PORT/devtools/...`. Cleaner alternative: have `ChromeHandle` carry the port explicitly. If you do, also update the extension `e2e_chrome` fixture for symmetry.)

**Step 7.3: Run, verify pass**

```bash
cd browser-daemon && uv run pytest tests/e2e/test_l0_smoke.py -v
```
Expected: all 3 tests pass.

**Step 7.4: Commit**

```bash
git add browser-daemon/tests/e2e/
git commit -m "test(e2e): L0 RDP backend resolves via daemon url"
```

### Task 8: L1 round-trip via skill CLI

**Files:**
- Create: `browser-daemon/tests/e2e/test_l1_roundtrip.py`

**Step 8.1: Write the tests**

```python
"""L1 — single round-trip through the skill CLI."""
from __future__ import annotations

import json

from .helpers import run_skill


def test_extension_backend_page_info(ext_ready):
    """`browser-skill <<PY ... PY` returns page_info() that the test can parse.
    Extension backend, attached to whatever default tab Chrome opened."""
    result = run_skill(
        script=(
            "import json\n"
            "info = page_info()\n"
            "print(json.dumps(info))\n"
        ),
        backend="extension",
    )
    assert result.returncode == 0, (
        f"skill exited {result.returncode}; "
        f"stdout={result.stdout!r} stderr={result.stderr!r}"
    )
    # The last JSON line in stdout is page_info; tolerate skill banner noise.
    line = next(
        ln for ln in reversed(result.stdout.strip().splitlines())
        if ln.startswith("{")
    )
    info = json.loads(line)
    assert isinstance(info, dict)
    assert "url" in info and "title" in info


def test_rdp_backend_page_info(e2e_chrome_rdp):
    result = run_skill(
        script=(
            "import json\n"
            "info = page_info()\n"
            "print(json.dumps(info))\n"
        ),
        backend="rdp",
    )
    assert result.returncode == 0, (
        f"skill exited {result.returncode}; "
        f"stdout={result.stdout!r} stderr={result.stderr!r}"
    )
    line = next(
        ln for ln in reversed(result.stdout.strip().splitlines())
        if ln.startswith("{")
    )
    info = json.loads(line)
    assert isinstance(info, dict)
    assert "url" in info and "title" in info
```

**Step 8.2: Run, verify pass**

```bash
cd browser-daemon && uv run pytest tests/e2e/test_l1_roundtrip.py -v
```
Expected: 2 passed.

> **Likely first-run failures:** skill banner format may break the JSON-extract loop (revisit the parse heuristic), or `page_info()` may need a tab to attach to first. If extension backend's "no attached tab" error fires, adjust the script to `open_background(...) → page_info()` instead — see L2 task body, same shape.

**Step 8.3: Commit**

```bash
git add browser-daemon/tests/e2e/test_l1_roundtrip.py
git commit -m "test(e2e): L1 page_info round-trip via skill (both backends)"
```

### Task 9: L2 user flows + artifact dumping

**Files:**
- Create: `browser-daemon/tests/e2e/test_l2_user_flows.py`
- Modify: `browser-daemon/tests/e2e/conftest.py` (add autouse artifact-dump fixture)

**Step 9.1: Artifact-dump fixture in conftest**

Append:

```python
@pytest.fixture(autouse=True)
def _e2e_dump_artifacts_on_failure(request, e2e_artifacts_dir):
    """When an `real_chrome` test fails, write env + daemon log path into
    `_artifacts/<nodeid>/`. Screenshots are written by the test itself when
    relevant; this fixture only copies inert state."""
    yield
    if request.node.rep_call.failed if hasattr(request.node, "rep_call") else False:
        outdir = e2e_artifacts_dir / request.node.name
        outdir.mkdir(parents=True, exist_ok=True)
        env_lines = [f"{k}={v}" for k, v in sorted(os.environ.items())
                     if k.startswith(("BD_", "BS_", "BU_"))]
        (outdir / "env.txt").write_text("\n".join(env_lines), encoding="utf-8")


# Standard pytest pattern to expose rep_call on the request node.
@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    outcome = yield
    rep = outcome.get_result()
    setattr(item, f"rep_{rep.when}", rep)
```

**Step 9.2: Write L2 tests**

```python
"""L2 — standard user flows through skill, extension backend (primary path)."""
from __future__ import annotations

import json

from .helpers import run_skill


def test_open_background_and_query_dom(ext_ready):
    script = (
        "import json\n"
        "h = open_background('data:text/html,<h1>e2e</h1>')\n"
        "wait_for_load()\n"
        "txt = js(\"document.querySelector('h1').textContent\")\n"
        "info = page_info()\n"
        "print(json.dumps({'text': txt, 'title': info.get('title'), 'url': info.get('url')}))\n"
    )
    result = run_skill(script=script, backend="extension")
    assert result.returncode == 0, (
        f"skill exited {result.returncode}; stderr={result.stderr!r}"
    )
    line = next(
        ln for ln in reversed(result.stdout.strip().splitlines())
        if ln.startswith("{")
    )
    payload = json.loads(line)
    assert payload["text"] == "e2e"
    assert payload["url"].startswith("data:text/html")


def test_screenshot_is_non_trivial(ext_ready, tmp_path):
    out_png = tmp_path / "shot.png"
    script = (
        f"open_background('data:text/html,<h1 style=\"font-size:120px\">SHOT</h1>')\n"
        "wait_for_load()\n"
        "from pathlib import Path\n"
        f"data = capture_screenshot()\n"
        f"Path({str(out_png)!r}).write_bytes(data if isinstance(data, (bytes, bytearray)) else data.read())\n"
    )
    result = run_skill(script=script, backend="extension", timeout=60)
    assert result.returncode == 0, (
        f"skill exited {result.returncode}; stderr={result.stderr!r}"
    )
    assert out_png.exists()
    size = out_png.stat().st_size
    # A black/blank PNG is typically <2KB. Real screenshot is usually >>5KB.
    assert size > 5_000, f"screenshot suspiciously small: {size}B"
```

> **Implementer note:** `capture_screenshot()` return type — verify against the skill's actual signature. The conditional `isinstance(...)` accepts either bytes or a file-like; trim once you confirm.

**Step 9.3: Force a failure to confirm artifacts**

Temporarily add (in a new file `tests/e2e/test_artifact_smoke.py`):

```python
def test_artifact_dump_works(ext_ready):
    """REMOVE BEFORE COMMITTING — temp test to confirm the artifact-dump
    autouse fixture writes env.txt on failure."""
    assert False, "intentional fail to test artifact dump"
```

Run:

```bash
cd browser-daemon && uv run pytest tests/e2e/test_artifact_smoke.py -v
```

Expected: 1 failed. Then verify:

```bash
ls browser-daemon/tests/e2e/_artifacts/test_artifact_dump_works/
# expect: env.txt
cat browser-daemon/tests/e2e/_artifacts/test_artifact_dump_works/env.txt
# expect: BD_NAME=bd-e2e, BS_DAEMON_BACKEND=..., etc.
```

Then DELETE the temp file:

```bash
rm browser-daemon/tests/e2e/test_artifact_smoke.py
rm -rf browser-daemon/tests/e2e/_artifacts/test_artifact_dump_works
```

**Step 9.4: Run real L2 + verify pass**

```bash
cd browser-daemon && uv run pytest tests/e2e/test_l2_user_flows.py -v
```
Expected: 2 passed.

**Step 9.5: Commit**

```bash
git add browser-daemon/tests/e2e/
git commit -m "test(e2e): L2 user flows + artifact dump on failure"
```

### Task 10: L3 cross-backend parity

**Files:**
- Create: `browser-daemon/tests/e2e/test_l3_parity.py`

**Step 10.1: Write parametrized parity test**

```python
"""L3 — same observable behaviour across backends."""
from __future__ import annotations

import json

import pytest

from .helpers import run_skill


PAGE = "data:text/html,<title>parity</title><h1 id=h>P</h1>"


def _extract_payload(stdout: str) -> dict:
    line = next(ln for ln in reversed(stdout.strip().splitlines()) if ln.startswith("{"))
    return json.loads(line)


@pytest.mark.parametrize("backend,fixture_name", [
    ("extension", "ext_ready"),
    ("rdp", "e2e_chrome_rdp"),
])
def test_dom_query_parity(backend, fixture_name, request):
    request.getfixturevalue(fixture_name)
    script = (
        "import json\n"
        f"open_background({PAGE!r})\n"
        "wait_for_load()\n"
        "txt = js(\"document.getElementById('h').textContent\")\n"
        "title = js(\"document.title\")\n"
        "print(json.dumps({'txt': txt, 'title': title}))\n"
    )
    result = run_skill(script=script, backend=backend)
    assert result.returncode == 0, result.stderr
    payload = _extract_payload(result.stdout)
    assert payload["txt"] == "P"
    assert payload["title"] == "parity"
```

> **Note:** RDP backend may not support `open_background` (per `browser-skill/ONBOARDING.md:149` —"new_tab() doesn't support the extension backend"). Read that file before writing this task; you may need to switch RDP to `new_tab()` and only use `open_background()` on extension. The intent of the parity test is **observable behaviour matches** — the route to get there can differ per backend.

**Step 10.2: Run, verify pass**

```bash
cd browser-daemon && uv run pytest tests/e2e/test_l3_parity.py -v
```
Expected: 2 passed (one per backend).

**Step 10.3: Commit**

```bash
git add browser-daemon/tests/e2e/test_l3_parity.py
git commit -m "test(e2e): L3 cross-backend behaviour parity"
```

### ⏸ Review checkpoint after Phase 3

Verify before continuing:
- `uv run pytest tests/` — inner loop still untouched.
- `uv run pytest tests/e2e/` — full E2E suite green (~30-60s wall time).
- All 4 L0-L3 test files produce sensible failure output when something is wrong (try `git stash` on the launch_chrome `extra_args` line to confirm L0 fails fast with a real message).
- `_artifacts/` after a clean run contains daemon.log + nothing else.
- No orphan Chrome / daemon processes after teardown.

---

## Phase 4 — Documentation and handoff

### Task 11: tests/e2e/README.md

**Files:**
- Create: `browser-daemon/tests/e2e/README.md`

**Content** (write this verbatim, adjust port numbers if Phase 2 deviates):

```markdown
# Real-extension E2E tests

These tests spin up a *real* Chrome with the locally-built extension loaded,
a *real* daemon, and drive them through the `browser-skill` CLI. They are
**opt-in** and **isolated** — they do not touch your daily Chrome or the
production daemon on port 19989.

## Running

    # Run the full E2E suite (~30-60s)
    cd browser-daemon
    uv run pytest tests/e2e/

    # Or by marker
    uv run pytest -m real_chrome

    # One file
    uv run pytest tests/e2e/test_l2_user_flows.py -v

The default `uv run pytest tests/` does NOT run these — they require a head
of display and a few seconds per case, which we keep out of the inner loop.

## Isolation matrix

| Dimension | Production (your daily) | Test (these E2Es) |
|---|---|---|
| daemon extension port | 19989 | 29989 |
| daemon RDP port | default | 29990 |
| daemon `BD_NAME` | `default` | `bd-e2e` |
| Chrome `user-data-dir` | your daily profile | per-test tmpdir |
| extension `RELAY_URL` | `:19989` (hardcoded) | `:29989` (patched copy) |
| daemon config path | `~/.config/browser-daemon` | `tmp_path` per session |

Nothing escapes the test boundary. You can have your daily Chrome + extension
running while these tests run.

## Artifacts on failure

When a test fails, `_artifacts/<test-name>/` contains:

- `env.txt` — relevant `BD_*` / `BS_*` env vars at run time
- (session-level) `daemon.log` — daemon stderr (everything)

This directory is gitignored.

## Adding a new test

1. Pick the right level (L0=smoke, L1=single round-trip, L2=user flow,
   L3=cross-backend parity).
2. If you need Chrome, depend on `ext_ready` (extension backend) or
   `e2e_chrome_rdp` (RDP backend).
3. To drive the skill, use `helpers.run_skill(script, backend=...)`.
4. Keep assertions at the observable level (page_info, DOM, screenshot),
   not at daemon-internal state — so v2's sub-agent harness can reuse them.

## When this fails

- "port 29989 already in use" → `lsof -i :29989`, kill the stale daemon.
- "extension never connected within 10s" → check `_artifacts/daemon.log`;
  most likely the patched `RELAY_URL` is wrong or Chrome failed to load the
  extension dir.
- Orphan Chrome after run → `pgrep -fa bd-e2e | xargs kill`.
```

**Step 11.1: Write the file**

(Use the content above.)

**Step 11.2: Commit**

```bash
git add browser-daemon/tests/e2e/README.md
git commit -m "docs(e2e): add tests/e2e/README"
```

### Task 12: Cross-link from project docs

**Files:**
- Modify: `browser-daemon/README.md` (add one paragraph)
- Modify: `browser-skill/README.md` (add one paragraph) — only if there's a natural slot

**Step 12.1: Append to `browser-daemon/README.md`**

Find a "Testing" or "Development" section if it exists; else append a top-level section near the bottom:

```markdown
## End-to-end tests with a real Chrome

If you edit the extension (`chrome-extension/background.js`) or daemon
internals, validate against a real Chrome:

    uv run pytest tests/e2e/

This spawns an isolated Chrome with a patched copy of the extension, talking
to a test daemon on port 29989. It will not touch your daily Chrome.

See `tests/e2e/README.md` for details.
```

**Step 12.2: Commit**

```bash
git add browser-daemon/README.md
git commit -m "docs: link to tests/e2e from browser-daemon README"
```

### Task 13: Final review checkpoint

Before declaring done:

1. **All tests green:**
   ```bash
   cd browser-daemon
   uv run pytest tests/        # inner loop
   uv run pytest tests/e2e/    # full E2E
   ```

2. **Clean process state:**
   ```bash
   pgrep -fa bd-e2e   # should be empty
   pgrep -fa "browser-daemon serve"   # only your daily one, if any
   ```

3. **Diff to production code is minimal:**
   ```bash
   git diff main -- browser-daemon/src/
   # Expected: ONLY the `extra_args` parameter on launch_chrome.py.
   ```

4. **Commits are atomic and conventionally-named:**
   ```bash
   git log --oneline main..HEAD
   # Expected: ~12 commits, all `feat(...)` / `test(e2e): ...` / `docs(...)`.
   ```

5. **Re-read the design doc** (`docs/plans/2026-05-19-real-extension-e2e-design.md`)
   and verify every numbered delivery step (§4) is covered:
   - Step 1: launch_chrome extra_args ✓ (Task 1)
   - Step 2: conftest + _patch_extension + helpers ✓ (Tasks 2-6)
   - Step 3: L0 smoke ✓ (Tasks 4-5 + 7)
   - Step 4: L1 round-trip ✓ (Task 8)
   - Step 5: L2 user flows + artifacts ✓ (Task 9)
   - Step 6: L3 parity ✓ (Task 10)
   - Step 7: tests/e2e/README ✓ (Task 11)
   - Step 8: project README cross-link ✓ (Task 12)

---

## Open issues to surface (not blockers)

These are explicitly deferred per §4 of the design doc, but worth noting in
the PR description:

- CI integration (GH Actions): xvfb on Linux runners needed for headed Chrome
  with extensions; deferred to a follow-up.
- v2 Claude Agent SDK sub-agent harness: design hooks left in place
  (session-scoped daemon, action-level assertions, `run_skill` is a thin
  wrapper). No code yet.
- Hot-reload of the extension across cases: deferred — current cold-start
  fixture is good enough and 100% deterministic.
- Multi-tab / iframe / download flows: not in L0-L3.

---

## Implementer's quick-reference

When debugging a stuck test:

| Symptom | Likely cause | Fix |
|---|---|---|
| `port 29989 in use` at fixture start | stale test daemon | `lsof -i :29989` → kill |
| `extension never connected` | RELAY_URL patch failed | inspect `patched_ext_dir/background.js` |
| Skill prints `DaemonUnavailable` | `BS_DAEMON_URL_CMD` wrong | check `run_skill` env dump |
| Chrome opens then `e2e_chrome` teardown leaves process | SIGTERM ignored | bump teardown wait, check `proc.poll()` |
| `capture_screenshot` returns tiny PNG | page blank at shot time | add `wait_for_load()` first |
| L3 RDP parity test errors on `open_background` | RDP backend doesn't support that primitive | switch to `new_tab()` per backend |
