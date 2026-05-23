"""rdp backend — HTTP discovery + Chrome 147+ 404 fallback."""
from __future__ import annotations

import pytest

from browserwright.daemon.backends import rdp as rdp_mod
from browserwright.daemon.backends.rdp import RdpBackend
from browserwright.daemon.config import load
from browserwright.daemon.errors import Unavailable


class _FakeAsyncClient:
    """Drop-in for httpx.AsyncClient that returns a fixed status / body."""

    def __init__(self, status: int, body: dict | None = None, raise_exc: Exception | None = None):
        self.status = status
        self.body = body or {}
        self.raise_exc = raise_exc

    def __call__(self, *a, **kw):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return None

    async def get(self, url):
        if self.raise_exc is not None:
            raise self.raise_exc
        return _FakeResp(self.status, self.body)


class _FakeResp:
    def __init__(self, status, body):
        self.status_code = status
        self._body = body

    def json(self):
        return self._body


def _backend(port: int = 9222) -> RdpBackend:
    cfg = load(env={}, cli_port=port)
    return RdpBackend(cfg)


# ---- 200 OK happy path -----------------------------------------------------

@pytest.mark.asyncio
async def test_resolve_200_returns_ws_url(monkeypatch):
    expected = "ws://127.0.0.1:9222/devtools/browser/uuid-xyz"
    monkeypatch.setattr(
        rdp_mod.httpx, "AsyncClient",
        _FakeAsyncClient(200, {"webSocketDebuggerUrl": expected}),
    )
    res = await _backend().resolve(timeout=2)
    assert res.ws_url == expected
    assert res.backend == "rdp"


@pytest.mark.asyncio
async def test_probe_200_marks_available(monkeypatch):
    monkeypatch.setattr(
        rdp_mod.httpx, "AsyncClient",
        _FakeAsyncClient(200, {"webSocketDebuggerUrl": "ws://127.0.0.1:9222/x"}),
    )
    d = await _backend().probe()
    assert d.available is True
    assert d.ws_url is None, "probe must never carry a ws_url (spec §5.2)"


# ---- 404 default-profile lockdown fallback ---------------------------------

@pytest.mark.asyncio
async def test_resolve_404_falls_back_to_devtools_active_port(monkeypatch, tmp_path):
    """Chrome 136/147+ default-profile lockdown: /json/version returns 404 but
    DevToolsActivePort still has the ws path. This is the must-have fallback
    (spec §8.2, ported from browser-harness daemon.py:83-101)."""
    # Build a fake profile dir layout under tmp_path and steer profile_paths()
    # to look there.
    base = tmp_path / "fake-profile"
    base.mkdir()
    (base / "DevToolsActivePort").write_text("9222\n/devtools/browser/from-file\n")
    monkeypatch.setattr(rdp_mod, "profile_paths", lambda: [base])

    monkeypatch.setattr(
        rdp_mod.httpx, "AsyncClient",
        _FakeAsyncClient(404),
    )
    res = await _backend(9222).resolve(timeout=2)
    assert res.ws_url == "ws://127.0.0.1:9222/devtools/browser/from-file"
    assert res.extras["isolated_profile"] is False, (
        "404 = default-profile lockdown, so user is on default profile"
    )
    assert res.extras["profile_path"] == str(base)


@pytest.mark.asyncio
async def test_resolve_404_no_matching_devtools_file_raises(monkeypatch, tmp_path):
    """404 + no DevToolsActivePort file in known profiles = truly unavailable."""
    monkeypatch.setattr(rdp_mod, "profile_paths", lambda: [tmp_path / "nothing-here"])
    monkeypatch.setattr(rdp_mod.httpx, "AsyncClient", _FakeAsyncClient(404))

    with pytest.raises(Unavailable):
        await _backend().resolve(timeout=2)


@pytest.mark.asyncio
async def test_resolve_404_wrong_port_in_file_is_ignored(monkeypatch, tmp_path):
    """A DevToolsActivePort file pointing at a *different* port must not match.
    This is the stale-file scenario from real life."""
    base = tmp_path / "stale"
    base.mkdir()
    (base / "DevToolsActivePort").write_text("9333\n/devtools/browser/stale\n")
    monkeypatch.setattr(rdp_mod, "profile_paths", lambda: [base])
    monkeypatch.setattr(rdp_mod.httpx, "AsyncClient", _FakeAsyncClient(404))

    with pytest.raises(Unavailable):
        await _backend(9222).resolve(timeout=2)


# ---- transport-level failure ----------------------------------------------

@pytest.mark.asyncio
async def test_probe_connection_refused_returns_unavailable_doctor(monkeypatch):
    import httpx
    err = httpx.ConnectError("connection refused")
    monkeypatch.setattr(rdp_mod.httpx, "AsyncClient", _FakeAsyncClient(0, raise_exc=err))
    d = await _backend().probe()
    assert d.available is False
    assert "9222" in d.detail
