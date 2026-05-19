"""launch-chrome — H9 install-wizard helper. End-to-end test uses a fake binary
that writes DevToolsActivePort and then sleeps."""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import pytest

import browser_daemon.launch_chrome as lc_mod
from browser_daemon.config import load
from browser_daemon.errors import ChromeBinaryNotFound, Unavailable


@pytest.fixture
def fake_chrome(tmp_path):
    """A short Python script that:
       1. Parses --user-data-dir / --remote-debugging-port from argv
       2. Writes <user-data-dir>/DevToolsActivePort with port + ws path
       3. Sleeps so the parent's `poll()` doesn't see early death
    Used in place of a real chrome binary.
    """
    binary = tmp_path / "fake-chrome.py"
    binary.write_text("""\
import sys, time, re, os
# v0.5 Task #12: discover_chrome_binary now validates the candidate by
# running `<binary> --version` and checking exit 0. A fake chrome that
# doesn't respond to --version gets rejected before reaching the spawn.
if "--version" in sys.argv:
    print("FakeChrome 148.0.7778.168")
    sys.exit(0)
user_data_dir = None
port = None
for a in sys.argv[1:]:
    if a.startswith("--user-data-dir="):
        user_data_dir = a.split("=", 1)[1]
    elif a.startswith("--remote-debugging-port="):
        port = a.split("=", 1)[1]
os.makedirs(user_data_dir, exist_ok=True)
with open(os.path.join(user_data_dir, "DevToolsActivePort"), "w") as f:
    f.write(f"{port}\\n/devtools/browser/fake-uuid-1234\\n")
time.sleep(60)  # don't exit; the parent will TERM us via the pid file
""")
    binary.chmod(0o755)

    # Wrap in a shebang shim so subprocess.Popen([path, ...]) invokes Python.
    shim = tmp_path / "chrome"
    shim.write_text(f"#!/bin/sh\nexec {sys.executable} {binary} \"$@\"\n")
    shim.chmod(0o755)
    return shim


@pytest.mark.asyncio
async def test_launch_chrome_end_to_end(monkeypatch, tmp_path, fake_chrome):
    """Spawn → poll DevToolsActivePort → output ws URL → write pid file."""
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "run"))

    cfg = load(env={"XDG_CACHE_HOME": str(tmp_path / "cache"),
                    "XDG_RUNTIME_DIR": str(tmp_path / "run")})
    out = await lc_mod.launch_chrome(
        cfg,
        profile="testprof",
        persistent=True,
        chrome_binary=str(fake_chrome),
        port=51234,
        timeout=10.0,
    )
    assert out["schema_version"] == 1
    assert out["backend"] == "rdp"
    assert out["ws_url"] == "ws://127.0.0.1:51234/devtools/browser/fake-uuid-1234"
    assert out["extras"]["isolated_profile"] is True
    pid = out["extras"]["pid"]
    pid_file = out["extras"]["pid_file"]
    assert pid_file is not None
    assert Path(pid_file).read_text().strip() == str(pid)

    # Cleanup: kill our fake chrome so the test doesn't leak a python sleep
    try:
        os.kill(pid, 15)
    except ProcessLookupError:
        pass


@pytest.mark.asyncio
async def test_launch_chrome_binary_not_found_raises(monkeypatch, tmp_path):
    monkeypatch.setattr(lc_mod, "discover_chrome_binary", lambda explicit: None)
    cfg = load(env={})
    with pytest.raises(ChromeBinaryNotFound):
        await lc_mod.launch_chrome(cfg, chrome_binary=None)


@pytest.mark.asyncio
async def test_launch_chrome_invalid_profile_name_raises(tmp_path, fake_chrome):
    """Spec §6.7 BD_NAME regex applies to --profile too."""
    cfg = load(env={})
    with pytest.raises(Exception):  # UserError, raised by check_name
        await lc_mod.launch_chrome(
            cfg,
            profile="../etc/passwd",
            chrome_binary=str(fake_chrome),
        )


@pytest.mark.asyncio
async def test_launch_chrome_falls_back_to_json_version_when_devtools_port_missing(
    monkeypatch, tmp_path, fake_chrome
):
    """macOS Chrome 148 quirk: when the user's primary Chrome is already
    running, a spawned isolated Chrome answers `/json/version` but never
    writes DevToolsActivePort. launch-chrome must accept the HTTP discovery
    as a secondary signal when --port was explicit.

    We simulate by making the fake-chrome script NOT write DevToolsActivePort,
    and mocking the httpx call to return a Chrome-shaped /json/version body.
    """
    # Build a fake chrome that never writes DevToolsActivePort but stays alive.
    silent_chrome = tmp_path / "silent-chrome.sh"
    # Task #12: handle --version separately so discover_chrome_binary
    # validation passes (otherwise we'd be rejected pre-spawn).
    silent_chrome.write_text(
        '#!/bin/sh\nif [ "$1" = "--version" ]; then '
        'echo "SilentChrome 148.0"; exit 0; fi\nexec sleep 60\n')
    silent_chrome.chmod(0o755)

    # Patch httpx so the secondary probe sees a healthy /json/version.
    class _Resp:
        status_code = 200
        def json(self):
            return {
                "webSocketDebuggerUrl":
                    "ws://127.0.0.1:51234/devtools/browser/fallback-uuid",
            }

    class _Client:
        def __init__(self, *a, **kw): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): pass
        async def get(self, url):
            assert url == "http://127.0.0.1:51234/json/version"
            return _Resp()

    monkeypatch.setattr(lc_mod, "httpx", type("H", (), {
        "AsyncClient": _Client, "HTTPError": Exception,
    }))

    cfg = load(env={"XDG_CACHE_HOME": str(tmp_path / "cache"),
                    "XDG_RUNTIME_DIR": str(tmp_path / "run")})
    out = await lc_mod.launch_chrome(
        cfg,
        profile="testprof",
        persistent=True,
        chrome_binary=str(silent_chrome),
        port=51234,  # explicit port — required for the fallback to trigger
        timeout=3.0,
    )
    assert out["ws_url"] == "ws://127.0.0.1:51234/devtools/browser/fallback-uuid"
    # Cleanup
    try:
        os.kill(out["extras"]["pid"], 15)
    except ProcessLookupError:
        pass


@pytest.mark.asyncio
async def test_launch_chrome_no_fallback_without_explicit_port(tmp_path):
    """When --port 0 (OS-picked), there's no way to fall back to /json/version
    because we don't know the port. The error message should be clear about
    BOTH signals having failed."""
    silent_chrome = tmp_path / "silent-chrome.sh"
    # Task #12: handle --version separately so discover_chrome_binary
    # validation passes (otherwise we'd be rejected pre-spawn).
    silent_chrome.write_text(
        '#!/bin/sh\nif [ "$1" = "--version" ]; then '
        'echo "SilentChrome 148.0"; exit 0; fi\nexec sleep 60\n')
    silent_chrome.chmod(0o755)

    cfg = load(env={"XDG_CACHE_HOME": str(tmp_path / "cache"),
                    "XDG_RUNTIME_DIR": str(tmp_path / "run")})
    with pytest.raises(Unavailable) as exc:
        await lc_mod.launch_chrome(
            cfg,
            profile="t",
            persistent=True,
            chrome_binary=str(silent_chrome),
            port=0,  # OS-picked → no /json/version fallback possible
            timeout=1.5,
        )
    # Should mention DevToolsActivePort. Should NOT mention an HTTP probe
    # (because we didn't try one — no port to probe).
    assert "DevToolsActivePort" in str(exc.value)


@pytest.mark.asyncio
async def test_launch_chrome_devtools_port_never_appears_raises(tmp_path):
    """Simulate Chrome dying before writing DevToolsActivePort."""
    dying = tmp_path / "dying-chrome.sh"
    # Task #12: handle --version (so we PASS the binary-validity check)
    # and then die on the actual spawn.
    dying.write_text(
        '#!/bin/sh\nif [ "$1" = "--version" ]; then '
        'echo "DyingChrome 1.0"; exit 0; fi\nexit 7\n')
    dying.chmod(0o755)

    cfg = load(env={"XDG_CACHE_HOME": str(tmp_path / "cache"),
                    "XDG_RUNTIME_DIR": str(tmp_path / "run")})
    with pytest.raises(Unavailable):
        await lc_mod.launch_chrome(
            cfg,
            profile="testprof",
            persistent=True,
            chrome_binary=str(dying),
            timeout=2.0,
        )


# ---- v0.4.1 Bug 2 + Bug 3: poll race + --remote-allow-origins=* ----------


@pytest.mark.asyncio
async def test_launch_chrome_passes_remote_allow_origins_flag(
    monkeypatch, tmp_path, fake_chrome,
):
    """Chrome 121+ rejects the ws upgrade with HTTP 403 unless
    `--remote-allow-origins=*` (or an explicit allow-list) is on the cmdline.
    Skill / cdp-use opens the ws with no Origin header, which Chrome's CSRF
    defense treats as denied. Spec was silent; field report (Skill team May
    2026) confirmed real Chrome rejects without it.
    """
    captured_args: list[str] = []

    class _SpyPopen:
        def __init__(self, args, **kw):
            captured_args.extend(args)
            self.args = args
            self.pid = 99999
            self.returncode = None
        def poll(self): return None
        def terminate(self): pass

    # Bypass Task #12 validation in this test — we want to capture launch
    # args without letting the validator's subprocess.run path race against
    # the spy. discover_chrome_binary returns the fake path directly.
    from pathlib import Path
    monkeypatch.setattr(lc_mod, "discover_chrome_binary",
                        lambda explicit: Path(str(fake_chrome)))
    # Stub Popen so we capture args without actually launching anything.
    monkeypatch.setattr("subprocess.Popen", _SpyPopen)
    cfg = load(env={"XDG_CACHE_HOME": str(tmp_path / "cache"),
                    "XDG_RUNTIME_DIR": str(tmp_path / "run")})
    with pytest.raises(Unavailable):
        await lc_mod.launch_chrome(
            cfg,
            profile="testprof",
            persistent=True,
            chrome_binary=str(fake_chrome),
            port=51234,
            timeout=0.2,  # let it bail fast — we only want to inspect args
        )
    assert "--remote-allow-origins=*" in captured_args


@pytest.mark.asyncio
async def test_launch_chrome_keeps_polling_when_launcher_proc_exits(
    monkeypatch, tmp_path,
):
    """Bug 2 fix: on Chrome 148 macOS, the launcher binary fork-exec's the
    real Chrome and then exits (code 126). The grandchild keeps running and
    writes DevToolsActivePort. Pre-fix, we raised the instant
    `proc.poll() is not None`, missing the grandchild's signal. The fix
    keeps polling DevToolsActivePort + /json/version until `timeout` and
    only raises Unavailable if BOTH signals stay absent.
    """
    # Build a chrome that:
    #  - exits immediately (simulates parent fork-exec hand-off)
    #  - but DevToolsActivePort appears in user-data-dir ~0.3s later (simulates
    #    the grandchild writing it). We can't actually spawn the grandchild in
    #    a portable shell script; instead, schedule a side-thread that writes
    #    the file after the parent dies.
    parent_quick_exit = tmp_path / "parent-fork-exit.sh"
    # Task #12: PASS --version so discover_chrome_binary doesn't reject us
    # pre-spawn; the test's whole point is what happens AFTER spawn.
    parent_quick_exit.write_text(
        '#!/bin/sh\nif [ "$1" = "--version" ]; then '
        'echo "ForkExitChrome 1.0"; exit 0; fi\nexit 126\n')
    parent_quick_exit.chmod(0o755)

    import asyncio
    async def _write_devtools_port_late():
        # Wait until the launch_chrome poller has had a chance to observe the
        # parent exit, then write the file (simulating grandchild output).
        await asyncio.sleep(0.4)
        # The cache-allocated profile path comes from a deterministic
        # computation. We can predict it by re-running the helper.
        from browser_daemon.platforms import cache_dir
        # XDG_CACHE_HOME is set via monkeypatch below
        d = cache_dir() / "profiles" / "fork-exit-prof"
        d.mkdir(parents=True, exist_ok=True)
        (d / "DevToolsActivePort").write_text("51234\n/devtools/browser/grandchild\n")

    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "run"))
    cfg = load(env={"XDG_CACHE_HOME": str(tmp_path / "cache"),
                    "XDG_RUNTIME_DIR": str(tmp_path / "run")})

    # Run both tasks concurrently.
    writer = asyncio.create_task(_write_devtools_port_late())
    out = await lc_mod.launch_chrome(
        cfg,
        profile="fork-exit-prof",
        persistent=True,
        chrome_binary=str(parent_quick_exit),
        port=51234,
        timeout=3.0,
    )
    await writer
    # The launcher exited with 126 but we still resolved successfully because
    # the grandchild's DevToolsActivePort eventually appeared.
    assert out["ws_url"] == "ws://127.0.0.1:51234/devtools/browser/grandchild"


@pytest.mark.asyncio
async def test_launch_chrome_dying_chrome_error_includes_exit_code(
    monkeypatch, tmp_path,
):
    """When the launcher exits AND no DevToolsActivePort/HTTP signal appears,
    the timeout error must mention the exit code so the user can tell apart
    'Chrome immediately died' (bad flags, SingletonLock) from 'Chrome alive
    but mute' (sandbox stuck etc)."""
    dying = tmp_path / "dying.sh"
    # Task #12: PASS --version validation; die only on actual spawn.
    dying.write_text(
        '#!/bin/sh\nif [ "$1" = "--version" ]; then '
        'echo "Dying42 1.0"; exit 0; fi\nexit 42\n')
    dying.chmod(0o755)
    cfg = load(env={"XDG_CACHE_HOME": str(tmp_path / "cache"),
                    "XDG_RUNTIME_DIR": str(tmp_path / "run")})
    with pytest.raises(Unavailable) as exc:
        await lc_mod.launch_chrome(
            cfg,
            profile="diag-prof",
            persistent=True,
            chrome_binary=str(dying),
            port=51234,
            timeout=1.0,
        )
    msg = str(exc.value)
    assert "exited with code 42" in msg
    assert "DevToolsActivePort" in msg


# ---- v0.5 Task #11: refuse user default profile --------------------------


@pytest.mark.asyncio
async def test_launch_chrome_refuses_user_default_profile(
    monkeypatch, tmp_path, fake_chrome,
):
    """The root cause of the 2026-05-18 popup storm: an earlier launch-chrome
    invocation hit `~/Library/Application Support/Google/Chrome` (the user's
    default profile) instead of an isolated one, leaving --remote-debugging-port
    flag baked into the daily Chrome's process. Guard refuses that target now.

    We simulate by setting up `cache_dir()` to coincide with the real default
    profile path — `_allocate_data_dir(profile=..., persistent=True)` resolves
    via `cache_dir() / "profiles" / <profile>`, NOT the platforms table — so
    instead we monkeypatch the platforms table to point at our fake cache
    path, making "isolated" match a "default" entry.
    """
    fake_default = tmp_path / "cache" / "browser-daemon" / "profiles" / "isolated"
    # Pretend our cache dir is registered in profile_paths().
    monkeypatch.setattr(
        "browser_daemon.launch_chrome.profile_paths",
        lambda: [fake_default],
    )
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    cfg = load(env={"XDG_CACHE_HOME": str(tmp_path / "cache"),
                    "XDG_RUNTIME_DIR": str(tmp_path / "run")})

    with pytest.raises(Exception) as exc:
        await lc_mod.launch_chrome(
            cfg,
            profile="isolated",
            persistent=True,
            chrome_binary=str(fake_chrome),
            port=51234,
            timeout=2.0,
        )
    assert "default profile" in str(exc.value)
    assert "BD_LAUNCH_CHROME_ALLOW_DEFAULT_PROFILE" in str(exc.value)


@pytest.mark.asyncio
async def test_launch_chrome_isolated_profile_passes_guard(
    monkeypatch, tmp_path, fake_chrome,
):
    """Sanity: when profile is NOT one of the platform defaults, the guard
    is a no-op and the normal happy path runs to completion."""
    # Pretend a totally different path is the default. Our `isolated`
    # cache path won't match.
    monkeypatch.setattr(
        "browser_daemon.launch_chrome.profile_paths",
        lambda: [tmp_path / "someone-elses-profile"],
    )
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    cfg = load(env={"XDG_CACHE_HOME": str(tmp_path / "cache"),
                    "XDG_RUNTIME_DIR": str(tmp_path / "run")})

    out = await lc_mod.launch_chrome(
        cfg,
        profile="totally-isolated",
        persistent=True,
        chrome_binary=str(fake_chrome),
        port=51234,
        timeout=5.0,
    )
    assert out["schema_version"] == 1
    # Cleanup.
    try:
        os.kill(out["extras"]["pid"], 15)
    except ProcessLookupError:
        pass


@pytest.mark.asyncio
async def test_launch_chrome_allow_default_profile_flag_unlocks(
    monkeypatch, tmp_path, fake_chrome,
):
    """Expert escape: --allow-default-profile / BD_LAUNCH_CHROME_ALLOW_DEFAULT_PROFILE=1
    bypasses the guard for the rare case someone deliberately wants their
    daily Chrome on CDP. Other-mode default-profile-target completes the
    full happy path.
    """
    fake_default = tmp_path / "cache" / "browser-daemon" / "profiles" / "isolated"
    monkeypatch.setattr(
        "browser_daemon.launch_chrome.profile_paths",
        lambda: [fake_default],
    )
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    cfg = load(env={"XDG_CACHE_HOME": str(tmp_path / "cache"),
                    "XDG_RUNTIME_DIR": str(tmp_path / "run")})

    out = await lc_mod.launch_chrome(
        cfg,
        profile="isolated",
        persistent=True,
        chrome_binary=str(fake_chrome),
        port=51234,
        timeout=5.0,
        allow_default_profile=True,
    )
    assert out["schema_version"] == 1
    try:
        os.kill(out["extras"]["pid"], 15)
    except ProcessLookupError:
        pass


@pytest.mark.asyncio
async def test_launch_chrome_env_var_unlocks_default_profile_guard(
    monkeypatch, tmp_path, fake_chrome,
):
    """The env var equivalent of --allow-default-profile."""
    fake_default = tmp_path / "cache" / "browser-daemon" / "profiles" / "isolated"
    monkeypatch.setattr(
        "browser_daemon.launch_chrome.profile_paths",
        lambda: [fake_default],
    )
    monkeypatch.setenv("BD_LAUNCH_CHROME_ALLOW_DEFAULT_PROFILE", "1")
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    cfg = load(env={"XDG_CACHE_HOME": str(tmp_path / "cache"),
                    "XDG_RUNTIME_DIR": str(tmp_path / "run")})

    out = await lc_mod.launch_chrome(
        cfg,
        profile="isolated",
        persistent=True,
        chrome_binary=str(fake_chrome),
        port=51234,
        timeout=5.0,
    )
    assert out["schema_version"] == 1
    try:
        os.kill(out["extras"]["pid"], 15)
    except ProcessLookupError:
        pass


# ---- E2E harness: extra_args parameter ------------------------------------


@pytest.mark.asyncio
async def test_launch_chrome_passes_extra_args(monkeypatch, tmp_path, fake_chrome):
    """extra_args list is appended verbatim to the Chrome argv."""
    captured: list[list[str]] = []

    real_popen = lc_mod.subprocess.Popen

    def fake_popen(args, **kw):
        args_list = list(args)
        # Only capture the real Chrome launch, not the --version validation call
        if "--version" not in args_list:
            captured.append(args_list)
        return real_popen(args, **kw)

    monkeypatch.setattr(lc_mod.subprocess, "Popen", fake_popen)
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "run"))

    cfg = load(env={"XDG_CACHE_HOME": str(tmp_path / "cache"),
                    "XDG_RUNTIME_DIR": str(tmp_path / "run")})
    out = await lc_mod.launch_chrome(
        cfg,
        profile="isolated",
        chrome_binary=str(fake_chrome),
        port=51234,
        extra_args=["--load-extension=/tmp/fake-ext", "--disable-features=Foo"],
    )
    assert out["schema_version"] == 1
    assert captured, "Popen never called"
    argv = captured[0]
    assert "--load-extension=/tmp/fake-ext" in argv
    assert "--disable-features=Foo" in argv
    # extra_args appended after the existing flags, not interleaved before them
    assert argv.index("--remote-allow-origins=*") < argv.index("--load-extension=/tmp/fake-ext")
    # Keychain prompt suppression: macOS won't ask for the login password.
    assert "--password-store=basic" in argv
    assert "--use-mock-keychain" in argv
    # Cleanup
    try:
        os.kill(out["extras"]["pid"], 15)
    except ProcessLookupError:
        pass


# ---- v0.5 Task #12: discover_chrome_binary validates --version -----------


def test_discover_chrome_binary_skips_wrapper_that_exits_nonzero(
    monkeypatch, tmp_path,
):
    """Reproduce the Homebrew wrapper bug: PATH points at a wrapper script
    that exec's a non-existent .app and exits 126. Pre-fix
    `discover_chrome_binary` returned this path; with validation, we skip
    it and fall back to the next candidate.
    """
    bad_wrapper = tmp_path / "fake-homebrew" / "bin" / "chromium"
    bad_wrapper.parent.mkdir(parents=True)
    bad_wrapper.write_text("#!/bin/sh\nexit 126\n")
    bad_wrapper.chmod(0o755)

    good = tmp_path / "real-chrome" / "Google Chrome"
    good.parent.mkdir(parents=True)
    good.write_text("#!/bin/sh\necho 'Google Chrome 148.0' && exit 0\n")
    good.chmod(0o755)

    # Force discover to look at our wrapper first via shutil.which mock,
    # then have the platform candidates include the working chrome.
    from browser_daemon import platforms as platforms_mod

    monkeypatch.setattr(platforms_mod, "shutil",
                        type("S", (), {"which": lambda name: (
                            str(bad_wrapper) if name == "chromium" else None
                        )}))
    monkeypatch.setattr(platforms_mod, "chrome_binary_candidates",
                        lambda: [good])
    found = platforms_mod.discover_chrome_binary()
    assert found == good, f"expected {good}, got {found}"


def test_discover_chrome_binary_explicit_path_validates_too(tmp_path):
    """`--chrome-binary /path/to/broken` must NOT silently succeed if the
    binary returns nonzero on `--version`. The function returns None and
    the caller surfaces ChromeBinaryNotFound."""
    broken = tmp_path / "broken-chrome"
    broken.write_text("#!/bin/sh\nexit 7\n")
    broken.chmod(0o755)
    from browser_daemon.platforms import discover_chrome_binary
    assert discover_chrome_binary(str(broken)) is None


def test_discover_chrome_binary_accepts_working_explicit_path(tmp_path):
    good = tmp_path / "good-chrome"
    good.write_text("#!/bin/sh\necho 'Chrome 1.0' && exit 0\n")
    good.chmod(0o755)
    from browser_daemon.platforms import discover_chrome_binary
    out = discover_chrome_binary(str(good))
    assert out == good


def test_discover_chrome_binary_returns_none_when_nothing_works(
    monkeypatch, tmp_path,
):
    """All candidates fail validation → None (caller raises ChromeBinaryNotFound)."""
    bad1 = tmp_path / "b1"
    bad1.write_text("#!/bin/sh\nexit 1\n")
    bad1.chmod(0o755)
    bad2 = tmp_path / "b2"
    bad2.write_text("#!/bin/sh\nexit 126\n")
    bad2.chmod(0o755)

    from browser_daemon import platforms as platforms_mod
    monkeypatch.setattr(platforms_mod, "shutil",
                        type("S", (), {"which": lambda name: str(bad1)}))
    monkeypatch.setattr(platforms_mod, "chrome_binary_candidates",
                        lambda: [bad2])
    assert platforms_mod.discover_chrome_binary() is None


# ---- v0.5.3 F-9 / bug #11: symlinked default profile ---------------------


@pytest.mark.asyncio
async def test_launch_chrome_refuses_default_profile_via_symlink(
    monkeypatch, tmp_path, fake_chrome,
):
    """F-9 / bug #11: previously used string-equality on Path strings.
    A symlink pointing at the platform-default profile path would slip
    through (different string, same inode). Fix: `Path.resolve(strict=False)`
    in `_check_not_default_profile` follows symlinks before compare.
    """
    # Set up: the "real" default profile lives at /tmp/...; our cache dir's
    # profile is a symlink pointing AT that real default.
    real_default = tmp_path / "real-default-profile"
    real_default.mkdir()
    cache_root = tmp_path / "cache" / "browser-daemon" / "profiles"
    cache_root.mkdir(parents=True)
    symlinked = cache_root / "isolated"
    symlinked.symlink_to(real_default)

    # Pretend `real-default-profile` is one of the platform defaults.
    monkeypatch.setattr(
        "browser_daemon.launch_chrome.profile_paths",
        lambda: [real_default],
    )
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    cfg = load(env={"XDG_CACHE_HOME": str(tmp_path / "cache"),
                    "XDG_RUNTIME_DIR": str(tmp_path / "run")})

    # `--profile isolated` points at the symlink → must be refused.
    with pytest.raises(Exception) as exc:
        await lc_mod.launch_chrome(
            cfg,
            profile="isolated",
            persistent=True,
            chrome_binary=str(fake_chrome),
            port=51234,
            timeout=2.0,
        )
    assert "default profile" in str(exc.value)


@pytest.mark.asyncio
async def test_launch_chrome_yes_env_unlocks_default_profile_guard(
    monkeypatch, tmp_path, fake_chrome,
):
    """F-9 / bug #11: `BD_LAUNCH_CHROME_ALLOW_DEFAULT_PROFILE=yes` (and
    other truthy strings) now unlock the guard. Test the `"yes"` variant
    specifically — the failure mode REVIEW.md called out."""
    fake_default = tmp_path / "cache" / "browser-daemon" / "profiles" / "isolated"
    monkeypatch.setattr(
        "browser_daemon.launch_chrome.profile_paths",
        lambda: [fake_default],
    )
    monkeypatch.setenv("BD_LAUNCH_CHROME_ALLOW_DEFAULT_PROFILE", "yes")
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    cfg = load(env={"XDG_CACHE_HOME": str(tmp_path / "cache"),
                    "XDG_RUNTIME_DIR": str(tmp_path / "run")})

    out = await lc_mod.launch_chrome(
        cfg,
        profile="isolated",
        persistent=True,
        chrome_binary=str(fake_chrome),
        port=51234,
        timeout=5.0,
    )
    assert out["schema_version"] == 1
    try:
        os.kill(out["extras"]["pid"], 15)
    except ProcessLookupError:
        pass
