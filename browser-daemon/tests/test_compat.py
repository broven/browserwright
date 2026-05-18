"""§9.5 compat cases — fingerprint-style rdp and cloud wss URL passthrough.

These two cases exercise the "many vendors, one daemon" claim from §4.4:
- fingerprint browsers (AdsPower / MultiLogin / GoLogin / 比特浏览器) run on
  arbitrary ports and a vendor-specific UA string, but they all speak the
  standard `/json/version` shape on a non-9222 port — `rdp --port N` covers it.
- cloud browsers (Browser Use / Browserless / Hyperbrowser) use URL-embedded
  tokens that must passthrough byte-for-byte.
"""
from __future__ import annotations

import asyncio
import json

import httpx
import pytest

import browser_daemon.backends.rdp as rdp_mod
from browser_daemon.backends.env import EnvBackend
from browser_daemon.backends.rdp import RdpBackend
from browser_daemon.config import load


@pytest.mark.asyncio
async def test_fingerprint_browser_style_rdp_non_default_port(monkeypatch):
    """AdsPower-style: port 51789, weird Browser banner, normal /json/version."""
    captured: list[str] = []

    class _Resp:
        status_code = 200
        def json(self):
            return {
                "Browser": "AdsPower-Chrome-114.0.5735.91",
                "Protocol-Version": "1.3",
                "User-Agent": "Mozilla/5.0 ... AdsPower",
                "webSocketDebuggerUrl":
                    "ws://127.0.0.1:51789/devtools/browser/adspower-uuid",
            }

    class _Client:
        def __init__(self, *a, **kw): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): pass
        async def get(self, url):
            captured.append(url)
            return _Resp()

    monkeypatch.setattr(rdp_mod.httpx, "AsyncClient", _Client)

    cfg = load(env={}, cli_port=51789)
    res = await RdpBackend(cfg).resolve(timeout=2)
    assert res.ws_url == "ws://127.0.0.1:51789/devtools/browser/adspower-uuid"
    assert captured == ["http://127.0.0.1:51789/json/version"]


@pytest.mark.asyncio
async def test_cloud_wss_with_url_embedded_token_passthrough():
    """spec §8.1.1: env backend MUST forward URL-embedded tokens unmodified —
    no rewriting, no stripping, no canonicalization. Cloud providers depend
    on it for auth."""
    raw = "wss://cloud.example.com/cdp/session-7?api_key=sk_live_abc123&region=us-east-1"
    cfg = load(env={"BD_CDP_WS": raw})
    res = await EnvBackend(cfg).resolve(timeout=2)
    assert res.ws_url == raw
    # Belt-and-suspenders: every chunk of the original query is preserved.
    assert "api_key=sk_live_abc123" in res.ws_url
    assert "region=us-east-1" in res.ws_url


@pytest.mark.asyncio
async def test_cloud_basic_auth_in_url_passthrough():
    """RFC-compliant basic auth in the URL must also passthrough."""
    raw = "wss://user:hunter2@example.com/cdp"
    cfg = load(env={"BD_CDP_WS": raw})
    res = await EnvBackend(cfg).resolve(timeout=2)
    assert res.ws_url == raw


@pytest.mark.asyncio
async def test_bu_compat_alias_still_works():
    """A user who hasn't migrated their shell rc yet still gets a working
    daemon — the legacy BU_CDP_WS path keeps cooking."""
    raw = "wss://legacy.example.com/cdp?t=old_token"
    cfg = load(env={"BU_CDP_WS": raw})
    res = await EnvBackend(cfg).resolve(timeout=2)
    assert res.ws_url == raw
    probe = await EnvBackend(cfg).probe()
    assert "BU_CDP_WS" in probe.detail
    assert "deprecated" in probe.detail.lower()
