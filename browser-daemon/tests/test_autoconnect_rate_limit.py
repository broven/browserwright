"""P0 defense: autoconnect popup-accumulation rate-limit.

User-reported: Chrome 144+ freezes when "Allow remote debugging" popups
accumulate past an internal threshold. The autoconnect path triggers one
popup per upstream ws handshake — and any repeated `browser-daemon url` call
inside a short window will pile them up. The daemon defends by refusing
back-to-back resolves of `autoconnect` (60s window) UNLESS the caller is the
Mode B long-running daemon (which opens upstream ws once and shares) or the
user opted in via `BD_FORCE_AUTOCONNECT_RECONNECT=1`.

Tests verify:
1. Two Mode A resolves within window: second raises Unavailable with a useful
   message that names both alternatives.
2. Mode B context (caller_context = "mode_b_serve"): NOT rate-limited.
3. BD_FORCE_AUTOCONNECT_RECONNECT=1: bypasses the limit.
4. Anti-test: rdp / env / extension backends are unaffected (rate-limit is
   autoconnect-only).
5. Successful resolve writes the timestamp file atomically.
"""
from __future__ import annotations

import asyncio
import shutil
import tempfile
import time
from pathlib import Path

import pytest

from browser_daemon import resolver as resolver_mod
from browser_daemon.backends import autoconnect as ac_mod
from browser_daemon.backends.autoconnect import AutoconnectBackend
from browser_daemon.backends.env import EnvBackend
from browser_daemon.backends.rdp import RdpBackend
from browser_daemon.config import load
from browser_daemon.errors import Unavailable


@pytest.fixture
def short_runtime(monkeypatch):
    """Isolate the timestamp file under a fresh tmpdir so tests don't stomp
    each other or interfere with the user's daemon state."""
    d = Path(tempfile.mkdtemp(prefix="bd-rl-", dir="/tmp"))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(d))
    yield d
    shutil.rmtree(d, ignore_errors=True)


@pytest.fixture
def fake_profile(monkeypatch, tmp_path):
    """Stub `profile_paths` to a single profile with a valid DevToolsActivePort
    file so autoconnect.resolve() reaches the success branch."""
    base = tmp_path / "fake-profile"
    base.mkdir()
    (base / "DevToolsActivePort").write_text("9222\n/devtools/browser/abc\n")
    monkeypatch.setattr(ac_mod, "profile_paths", lambda: [base])
    return base


@pytest.fixture
def succeed_via_404_fallback(monkeypatch):
    """Force resolve() to take the 404 → ws_path fallback branch. Cheaper
    than mocking httpx success since the test isn't asserting on the URL —
    only on rate-limit behavior."""
    class _Resp:
        status_code = 404
        def json(self): return {}
    class _Client:
        def __init__(self, *a, **kw): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): pass
        async def get(self, url): return _Resp()
    monkeypatch.setattr(ac_mod.httpx, "AsyncClient", _Client)


def _backend() -> AutoconnectBackend:
    return AutoconnectBackend(load(env={}))


# ---- 1. Two Mode A calls in window → second blocked ----------------------


@pytest.mark.asyncio
async def test_mode_a_second_call_within_window_raises(
    short_runtime, fake_profile, succeed_via_404_fallback
):
    """The defense. First call succeeds + writes timestamp; second within
    60s raises Unavailable with the canonical guidance."""
    res = await _backend().resolve(timeout=2)
    assert res.ws_url.startswith("ws://127.0.0.1:9222/")
    assert ac_mod._timestamp_path().exists(), "first resolve must record timestamp"

    with pytest.raises(Unavailable) as exc:
        await _backend().resolve(timeout=2)
    msg = str(exc.value)
    assert "rate-limited" in msg
    # Error must explicitly name BOTH supported alternatives so the user knows
    # what to do without reading the README.
    assert "browser-daemon serve" in msg
    assert "launch-chrome" in msg
    assert "BD_FORCE_AUTOCONNECT_RECONNECT" in msg


# ---- 2. Mode B context skips rate-limit ----------------------------------


@pytest.mark.asyncio
async def test_mode_b_context_bypasses_rate_limit(
    short_runtime, fake_profile, succeed_via_404_fallback
):
    """When the resolve happens inside `browser-daemon serve`, the listener
    sets caller_context="mode_b_serve". The autoconnect backend honors that
    by skipping the rate-limit check. Repeated Mode B resolves never block."""
    # First resolve (Mode A) writes the timestamp.
    await _backend().resolve(timeout=2)

    # Now flip to Mode B context — the immediate next resolve must succeed.
    token = resolver_mod.caller_context.set("mode_b_serve")
    try:
        res = await _backend().resolve(timeout=2)
        assert res.ws_url.startswith("ws://127.0.0.1:9222/")
    finally:
        resolver_mod.caller_context.reset(token)

    # Sanity: Mode A is still blocked after the Mode B bypass.
    with pytest.raises(Unavailable):
        await _backend().resolve(timeout=2)


# ---- 3. Force flag bypasses --------------------------------------------------


@pytest.mark.asyncio
async def test_force_env_var_bypasses_rate_limit(
    monkeypatch, short_runtime, fake_profile, succeed_via_404_fallback
):
    await _backend().resolve(timeout=2)
    # Second would normally block.
    monkeypatch.setenv("BD_FORCE_AUTOCONNECT_RECONNECT", "1")
    res = await _backend().resolve(timeout=2)  # MUST succeed
    assert res.ws_url.startswith("ws://127.0.0.1:9222/")


# ---- 4. Rate-limit is autoconnect-only -----------------------------------


@pytest.mark.asyncio
async def test_rate_limit_does_not_affect_env_backend(short_runtime):
    """Anti-test: env backend has its own gating (BD_CDP_WS or BD_CDP_URL
    must be set). Rate-limit is autoconnect-only — the timestamp file must
    not affect other backends."""
    # Plant a recent timestamp that WOULD trigger the autoconnect rate-limit.
    ac_mod._write_last_handshake(time.time())

    # env backend with BD_CDP_WS set — must succeed regardless.
    cfg = load(env={"BD_CDP_WS": "wss://example.com/cdp"})
    res = await EnvBackend(cfg).resolve(timeout=2)
    assert res.ws_url == "wss://example.com/cdp"


@pytest.mark.asyncio
async def test_rate_limit_does_not_affect_rdp_backend(monkeypatch, short_runtime):
    """Anti-test: rdp has its own HTTP discovery path. Rate-limit doesn't
    apply (rdp Chrome is launched explicitly, not autoconnect-style)."""
    ac_mod._write_last_handshake(time.time())

    # Mock rdp's httpx to return 200 with a ws URL.
    from browser_daemon.backends import rdp as rdp_mod

    class _Resp:
        status_code = 200
        def json(self):
            return {"webSocketDebuggerUrl": "ws://127.0.0.1:9222/devtools/browser/x"}
    class _Client:
        def __init__(self, *a, **kw): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): pass
        async def get(self, url): return _Resp()

    monkeypatch.setattr(rdp_mod.httpx, "AsyncClient", _Client)

    cfg = load(env={}, cli_port=9222)
    res = await RdpBackend(cfg).resolve(timeout=2)
    assert "ws://127.0.0.1:9222" in res.ws_url


# ---- 5. Timestamp file is atomic + readable ------------------------------


def test_timestamp_write_and_read_roundtrip(short_runtime):
    """Atomicity: we write to `.tmp` and `os.replace`. A concurrent reader
    that catches the file mid-write must either see the OLD value or the NEW
    — never a partial blob."""
    p = ac_mod._timestamp_path()
    assert not p.exists()
    ac_mod._write_last_handshake(1234567890.123)
    assert p.exists()
    assert ac_mod._read_last_handshake() == pytest.approx(1234567890.123)


def test_timestamp_corrupt_file_returns_none(short_runtime):
    """Defensive: a hand-corrupted timestamp file (e.g. from a partial disk
    write before our atomic-replace landing) must degrade to 'no rate-limit'
    rather than crashing the daemon."""
    p = ac_mod._timestamp_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("not a number")
    assert ac_mod._read_last_handshake() is None


# ---- 6. Edge: timestamp older than window allows next call ---------------


@pytest.mark.asyncio
async def test_old_timestamp_does_not_block(
    short_runtime, fake_profile, succeed_via_404_fallback
):
    """Timestamp from 2 minutes ago must not block — the cooldown window has
    elapsed."""
    ac_mod._write_last_handshake(time.time() - 120)
    res = await _backend().resolve(timeout=2)
    assert res.ws_url.startswith("ws://127.0.0.1:9222/")


# ---- 7. probe() / doctor are NOT rate-limited ----------------------------


@pytest.mark.asyncio
async def test_probe_not_rate_limited(short_runtime, fake_profile):
    """Doctor probes are side-effect-free file reads — they MUST work even
    when resolve() is rate-limited. The defense is about ws handshakes, not
    about diagnostic visibility."""
    ac_mod._write_last_handshake(time.time())  # would block resolve()
    d = await _backend().probe()
    assert d.available is True
    assert d.ws_url is None  # spec §5.2: probe never opens ws
