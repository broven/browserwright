"""autoconnect backend — multi-profile mtime tie-break + zero ws side effects."""
from __future__ import annotations

import asyncio
import time
from pathlib import Path

import pytest

from browser_daemon.backends import autoconnect as ac_mod
from browser_daemon.backends.autoconnect import AutoconnectBackend
from browser_daemon.config import load


def _backend() -> AutoconnectBackend:
    cfg = load(env={})
    return AutoconnectBackend(cfg)


def _write_devtools_port(base: Path, port: str, ws_path: str, mtime: float | None = None):
    base.mkdir(parents=True, exist_ok=True)
    f = base / "DevToolsActivePort"
    f.write_text(f"{port}\n{ws_path}\n")
    if mtime is not None:
        import os
        os.utime(f, (mtime, mtime))


# ---- probe: zero ws side effects -------------------------------------------

@pytest.mark.asyncio
async def test_probe_does_not_open_any_ws(monkeypatch, tmp_path):
    """Spec §5.2 + §9.4 反测试: doctor must NEVER open a ws. We check by
    forcing the probe path to be taken and asserting our (mocked) ws sentinel
    is never touched."""
    base = tmp_path / "p"
    _write_devtools_port(base, "9222", "/devtools/browser/abc")
    monkeypatch.setattr(ac_mod, "profile_paths", lambda: [base])

    # Block the HTTP probe so we can be sure probe() returns without it
    class _NoCall:
        def __init__(self, *a, **kw): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): pass
        async def get(self, url):
            # Allowed (HTTP only, no ws). Return a normal-looking 200.
            class R:
                status_code = 200
            return R()
    monkeypatch.setattr(ac_mod.httpx, "AsyncClient", _NoCall)

    ws_opens: list[str] = []

    async def fake_ws_connect(*a, **kw):
        ws_opens.append(a[0] if a else "")
        raise AssertionError("websockets.connect must not be called by probe()")

    # Patch websockets.connect in case any code path stumbles into it.
    import websockets
    monkeypatch.setattr(websockets, "connect", fake_ws_connect, raising=False)

    d = await _backend().probe()
    assert d.available is True
    assert d.ws_url is None
    assert ws_opens == [], "probe must not open any ws"


# ---- probe: missing ---------------------------------------------------------

@pytest.mark.asyncio
async def test_probe_no_profiles_marks_unavailable(monkeypatch):
    monkeypatch.setattr(ac_mod, "profile_paths", lambda: [])
    d = await _backend().probe()
    assert d.available is False
    assert "enable chrome://inspect" in d.detail.lower()
    # ux_warning + needs_user_action MUST be populated even when unavailable —
    # spec §5.2 contract is that the install wizard can render the row.
    assert d.ux_warning is not None
    assert d.needs_user_action is not None


# ---- resolve: mtime newest --------------------------------------------------

@pytest.mark.asyncio
async def test_resolve_picks_mtime_newest_profile(monkeypatch, tmp_path):
    """Multi-profile case: older profile's port shouldn't win even if it's
    earlier in the registry list. spec §8.3 explicit rule: mtime newest."""
    # Isolate the autoconnect rate-limit timestamp file under tmp_path so
    # this test isn't poisoned by a recent run of test_autoconnect_rate_limit
    # (both share `/tmp/browser-daemon-autoconnect-last.ts` via runtime_dir()
    # when XDG_RUNTIME_DIR is unset).
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))

    old = tmp_path / "old-profile"
    new = tmp_path / "new-profile"
    _write_devtools_port(old, "9222", "/devtools/browser/old", mtime=time.time() - 600)
    _write_devtools_port(new, "9333", "/devtools/browser/new", mtime=time.time())

    monkeypatch.setattr(ac_mod, "profile_paths", lambda: [old, new])

    # Resolve via /json/version should hit `new` first because we sort by mtime
    # before iterating. We force HTTP to 404 to exercise the line-2 fallback,
    # which is also the realistic Chrome 147+ default-profile path.
    class _Resp:
        status_code = 404
    class _Client:
        def __init__(self, *a, **kw): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): pass
        async def get(self, url): return _Resp()
    monkeypatch.setattr(ac_mod.httpx, "AsyncClient", _Client)

    res = await _backend().resolve(timeout=2)
    assert "9333" in res.ws_url
    assert "old" not in res.ws_url
    assert res.extras["profile_path"] == str(new)


# ---- doctor schema lock ----------------------------------------------------

@pytest.mark.asyncio
async def test_doctor_entry_carries_ux_warning_and_action(monkeypatch, tmp_path):
    """Even on the happy path, autoconnect MUST report its popup warning —
    Skill renders this directly into install-wizard copy."""
    base = tmp_path / "p"
    _write_devtools_port(base, "9222", "/devtools/browser/x")
    monkeypatch.setattr(ac_mod, "profile_paths", lambda: [base])

    class _Resp:
        status_code = 200
    class _Client:
        def __init__(self, *a, **kw): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): pass
        async def get(self, url): return _Resp()
    monkeypatch.setattr(ac_mod.httpx, "AsyncClient", _Client)

    d = await _backend().probe()
    assert d.ux_warning is not None
    assert d.needs_user_action is not None
    assert d.ux_cost == "popup-per-ws+banner"
