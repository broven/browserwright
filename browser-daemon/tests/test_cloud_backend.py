"""v0.5 cloud backend tests.

Three angles:
  1. probe() — config validation + HTTP /__json/version reachability (with
     headers when the provider supplies them). Per spec §5.2: zero ws side
     effects.
  2. resolve() — straight ws URLs pass through; HTTP endpoints get discovery
     done with the auth header attached.
  3. fallback chain: `cloud` is NOT in the auto chain — only triggered via
     explicit `--backend cloud` / `BD_BACKEND=cloud`. A stale config row
     must never silently leak through `browser-daemon url`.
"""
from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from browser_daemon.backends.cloud import CloudBackend
from browser_daemon.config import load
from browser_daemon.errors import Unavailable, UserError
from browser_daemon.resolver import resolve as resolver_resolve


# ---- shared helpers -------------------------------------------------------


def _cfg_with(**kw) -> "Config":
    """Build a Config with `[backends.cloud].*` populated via kwargs."""
    cfg = load(env={})
    for k, v in kw.items():
        setattr(cfg.backends.cloud, k, v)
    return cfg


class _FakeHttpClient:
    """Mock httpx.AsyncClient that captures GET headers + URL and returns a
    canned response. Used to drive probe()/resolve() without real network."""

    captured: dict[str, Any] = {}

    def __init__(self, status: int, json_body: dict | None = None,
                 raise_exc: type[Exception] | None = None):
        self._status = status
        self._body = json_body or {}
        self._raise = raise_exc

    @classmethod
    def factory(cls, **kw):
        def make(*a, **kwargs):
            return cls(**kw)
        return make

    def __call__(self, *a, **kw):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        pass

    async def get(self, url, headers=None, **kw):
        if self._raise is not None:
            raise self._raise("mock error")
        type(self).captured = {"url": url, "headers": dict(headers or {})}

        class _R:
            status_code = self._status

            def __init__(self, body):
                self._body = body

            def json(self):
                return self._body
        return _R(self._body)


# ---- probe ----------------------------------------------------------------


@pytest.mark.asyncio
async def test_probe_unavailable_when_endpoint_unset():
    cfg = _cfg_with()
    backend = CloudBackend(cfg)
    r = await backend.probe()
    assert r.available is False
    # v0.5 final doctor contract: "not configured; ..." replaces the
    # earlier "no cloud endpoint configured" phrasing.
    assert "not configured" in r.detail
    assert r.needs_user_action is not None


@pytest.mark.asyncio
async def test_probe_ws_endpoint_available_when_configured():
    """Direct ws:// endpoint + bearer token + provider_hint → available.
    v0.5 final contract: probe does NO network I/O, just config sanity."""
    cfg = _cfg_with(
        endpoint="wss://api.example/cdp",
        auth_kind="bearer",
        auth={"token": "T"},
        provider_hint="browser-use",
    )
    backend = CloudBackend(cfg)
    r = await backend.probe()
    assert r.available is True
    assert "browser-use" in r.detail
    assert "auth_kind=bearer" in r.detail
    # endpoint surfaced via extras (skill install-wizard reads this).
    assert r.extras["endpoint"] == "wss://api.example/cdp"


# Note: the old Phase-2 tests that drove `probe()` through a mock httpx and
# asserted on HTTP 401 / ConnectError behavior have been retired. The v0.5
# final doctor contract requires probe to be ZERO-network — those 401 /
# unreachable paths now live in the resolve() tests below, which is the
# right place: `resolve()` is allowed network I/O (HTTP discovery only,
# still no ws), `probe()` is not.


@pytest.mark.asyncio
async def test_probe_invalid_auth_config_surfaces_misconfig(monkeypatch):
    """If auth_kind is set but its env var isn't, probe should surface the
    auth misconfig in `detail` — not crash, not lie 'unavailable'."""
    monkeypatch.delenv("MISSING_TOKEN", raising=False)
    cfg = _cfg_with(
        endpoint="https://api.example.com",
        auth_kind="bearer",
        auth={"token_env": "MISSING_TOKEN"},
    )
    r = await CloudBackend(cfg).probe()
    assert r.available is False
    assert "auth" in r.detail.lower()


@pytest.mark.asyncio
async def test_probe_unknown_auth_kind_in_config_surfaces_misconfig():
    cfg = _cfg_with(
        endpoint="https://api.example.com",
        auth_kind="hmac",  # not a real kind
        auth={},
    )
    r = await CloudBackend(cfg).probe()
    assert r.available is False
    assert "auth misconfigured" in r.detail


# ---- resolve --------------------------------------------------------------


@pytest.mark.asyncio
async def test_resolve_ws_endpoint_returns_url_unchanged():
    cfg = _cfg_with(
        endpoint="wss://api/cdp",
        auth_kind="bearer",
        auth={"token": "T"},
    )
    res = await CloudBackend(cfg).resolve(timeout=5)
    assert res.ws_url == "wss://api/cdp"
    assert res.backend == "cloud"
    assert res.extras["auth_kind"] == "bearer"


@pytest.mark.asyncio
async def test_resolve_ws_endpoint_with_basic_auth_url_embeds():
    """Basic auth in Mode A path: even with header-mode default, Mode A
    has no header injection point, so `url_with_auth()` embeds creds."""
    cfg = _cfg_with(
        endpoint="wss://api.example/cdp",
        auth_kind="basic",
        auth={"username": "u", "password": "p"},
    )
    res = await CloudBackend(cfg).resolve(timeout=5)
    assert res.ws_url.startswith("wss://u:p@api.example/cdp")


@pytest.mark.asyncio
async def test_resolve_http_endpoint_walks_discovery_with_header(monkeypatch):
    monkeypatch.setattr(
        "browser_daemon.backends.cloud.httpx.AsyncClient",
        _FakeHttpClient.factory(
            status=200,
            json_body={"webSocketDebuggerUrl": "wss://api/cdp/uuid"},
        ),
    )
    cfg = _cfg_with(
        endpoint="https://api.example/",
        auth_kind="bearer",
        auth={"token": "tok-99"},
    )
    res = await CloudBackend(cfg).resolve(timeout=5)
    assert res.ws_url == "wss://api/cdp/uuid"
    # The discovery GET carried the bearer header.
    assert _FakeHttpClient.captured["headers"]["Authorization"] == "Bearer tok-99"


@pytest.mark.asyncio
async def test_resolve_missing_endpoint_raises_unavailable():
    cfg = _cfg_with()
    with pytest.raises(Unavailable) as exc:
        await CloudBackend(cfg).resolve(timeout=5)
    assert "no endpoint" in str(exc.value)


@pytest.mark.asyncio
async def test_resolve_http_404_raises_unavailable(monkeypatch):
    monkeypatch.setattr(
        "browser_daemon.backends.cloud.httpx.AsyncClient",
        _FakeHttpClient.factory(status=404),
    )
    cfg = _cfg_with(
        endpoint="https://nope/",
        auth_kind="bearer", auth={"token": "x"},
    )
    with pytest.raises(Unavailable) as exc:
        await CloudBackend(cfg).resolve(timeout=5)
    assert "404" in str(exc.value)


@pytest.mark.asyncio
async def test_resolve_no_websocket_url_in_response_raises(monkeypatch):
    monkeypatch.setattr(
        "browser_daemon.backends.cloud.httpx.AsyncClient",
        _FakeHttpClient.factory(status=200, json_body={"other": "field"}),
    )
    cfg = _cfg_with(
        endpoint="https://x/",
        auth_kind="bearer", auth={"token": "y"},
    )
    with pytest.raises(Unavailable) as exc:
        await CloudBackend(cfg).resolve(timeout=5)
    assert "webSocketDebuggerUrl" in str(exc.value)


# ---- fallback chain opt-out ------------------------------------------------


@pytest.mark.asyncio
async def test_cloud_backend_not_in_auto_fallback_chain(monkeypatch):
    """Critical safety invariant: `browser-daemon url` with no explicit
    `--backend` MUST NOT fall back to `cloud` even if a stale
    `[backends.cloud]` row sits in config.toml. Otherwise users with old
    config could silently authenticate against the wrong service.

    We patch the env/rdp/autoconnect backends to all raise Unavailable
    and assert the resolver's aggregated error does NOT mention "cloud".
    """
    from browser_daemon.errors import Unavailable

    async def boom(*a, **kw):
        raise Unavailable("nope", attempts={})

    # Patch every Unavailable-raiser; cloud is configured, env/rdp/autoconnect
    # all fail → resolver should aggregate WITHOUT consulting cloud.
    monkeypatch.setattr(
        "browser_daemon.backends.env.EnvBackend.resolve", boom)
    monkeypatch.setattr(
        "browser_daemon.backends.rdp.RdpBackend.resolve", boom)
    monkeypatch.setattr(
        "browser_daemon.backends.autoconnect.AutoconnectBackend.resolve", boom)

    cloud_called = {"flag": False}

    async def cloud_boom(*a, **kw):
        cloud_called["flag"] = True
        raise Unavailable("if you see this the chain skip is broken",
                          attempts={})

    monkeypatch.setattr(
        "browser_daemon.backends.cloud.CloudBackend.resolve", cloud_boom)

    cfg = _cfg_with(
        endpoint="wss://api/cdp",
        auth_kind="bearer", auth={"token": "T"},
    )
    cfg.backend = None  # auto chain
    with pytest.raises(Unavailable):
        await resolver_resolve(cfg)
    assert cloud_called["flag"] is False, "cloud backend was invoked in auto chain"


@pytest.mark.asyncio
async def test_cloud_backend_explicit_flag_still_works(monkeypatch):
    """The opt-out is from auto-fallback only. `--backend cloud` (explicit)
    still routes to it normally."""
    cfg = _cfg_with(
        endpoint="wss://api/cdp",
        auth_kind="bearer", auth={"token": "T"},
    )
    cfg.backend = "cloud"
    res = await resolver_resolve(cfg)
    assert res.backend == "cloud"
    assert res.ws_url == "wss://api/cdp"


# ---- config / env override -----------------------------------------------


def test_bd_cloud_env_vars_populate_config(monkeypatch):
    """`BD_CLOUD_ENDPOINT` / `BD_CLOUD_AUTH_KIND` / `BD_CLOUD_PROVIDER_HINT`
    are the env-level shortcuts for one-off CLI runs without writing
    config.toml. (Auth payload still flows through provider-specific env
    vars like `token_env`)."""
    cfg = load(env={
        "BD_CLOUD_ENDPOINT": "wss://x/cdp",
        "BD_CLOUD_AUTH_KIND": "bearer",
        "BD_CLOUD_PROVIDER_HINT": "browser-use",
    })
    assert cfg.backends.cloud.endpoint == "wss://x/cdp"
    assert cfg.backends.cloud.auth_kind == "bearer"
    assert cfg.backends.cloud.provider_hint == "browser-use"


def test_toml_cloud_config_round_trip(tmp_path, monkeypatch):
    """Round-trip a real toml file with `[backends.cloud]` + nested
    `[backends.cloud.auth.bearer]` to make sure the dispatch by auth_kind
    picks the right subtable into `cfg.backends.cloud.auth`."""
    toml = tmp_path / "config.toml"
    toml.write_text("""\
[backends.cloud]
endpoint = "wss://api/cdp"
auth_kind = "bearer"
provider_hint = "browser-use"

[backends.cloud.auth.bearer]
token_env = "MY_TOKEN"
header_name = "Authorization"
header_prefix = "Bearer "

[backends.cloud.auth.basic]
username_env = "USER"
password_env = "PASS"
""")
    cfg = load(env={"BD_CONFIG": str(toml)})
    assert cfg.backends.cloud.endpoint == "wss://api/cdp"
    assert cfg.backends.cloud.auth_kind == "bearer"
    # Only the matching subtable is selected.
    assert cfg.backends.cloud.auth == {
        "token_env": "MY_TOKEN",
        "header_name": "Authorization",
        "header_prefix": "Bearer ",
    }


# ---- v0.5 doctor JSON contract (skill install-wizard mirror) -------------


@pytest.mark.asyncio
async def test_probe_ux_cost_is_auth_required():
    """v0.5 new enum value. Skill install wizard reads ux_cost to render
    badges; `auth-required` distinguishes cloud from rdp (none) / autoconnect
    (popup) / extension (extension-permission)."""
    cfg = _cfg_with(
        endpoint="wss://x/cdp",
        auth_kind="bearer",
        auth={"token": "T"},
    )
    r = await CloudBackend(cfg).probe()
    assert r.ux_cost == "auth-required"


@pytest.mark.asyncio
async def test_probe_extras_contract_when_configured():
    """Doctor contract: extras carry {provider, endpoint, auth_kind, configured=True}
    when both endpoint + auth_kind are set and the AuthProvider loads."""
    cfg = _cfg_with(
        endpoint="wss://api.browser-use.com/cdp",
        auth_kind="bearer",
        auth={"token": "T"},
        provider_hint="browser-use",
    )
    r = await CloudBackend(cfg).probe()
    assert r.available is True
    assert r.extras == {
        "provider": "browser-use",
        "endpoint": "wss://api.browser-use.com/cdp",
        "auth_kind": "bearer",
        "configured": True,
    }


@pytest.mark.asyncio
async def test_probe_extras_contract_when_unconfigured():
    """No endpoint → extras carry configured=False so skill knows to prompt."""
    cfg = _cfg_with()
    r = await CloudBackend(cfg).probe()
    assert r.available is False
    assert "not configured" in r.detail
    assert r.extras == {
        "provider": "generic",  # default when provider_hint unset
        "endpoint": None,
        "auth_kind": None,
        "configured": False,
    }


@pytest.mark.asyncio
async def test_probe_does_not_open_network_connection_to_cloud(monkeypatch):
    """v0.5 doctor contract reinforces spec §H3: probe NEVER opens a
    ws / TCP connect to the cloud endpoint. Previously we did an HTTP GET
    to /json/version which crossed the network — fixed in v0.5 final.

    We patch httpx so the test fails loud if probe touches it.
    """
    called = {"flag": False}

    class _Boom:
        def __init__(self, *a, **kw): pass
        async def __aenter__(self):
            called["flag"] = True
            raise AssertionError("probe must not open network connections")
        async def __aexit__(self, *a): pass
        async def get(self, *a, **kw): pass

    monkeypatch.setattr(
        "browser_daemon.backends.cloud.httpx.AsyncClient", _Boom)

    cfg = _cfg_with(
        endpoint="https://api.example/",
        auth_kind="bearer",
        auth={"token": "T"},
    )
    r = await CloudBackend(cfg).probe()
    assert r.available is True
    assert called["flag"] is False


@pytest.mark.asyncio
async def test_probe_extras_surface_auth_misconfig(monkeypatch):
    """When auth provider fails to load (e.g., missing token_env), extras
    must say `configured=False` so install wizard catches the user halfway."""
    monkeypatch.delenv("MISSING_X", raising=False)
    cfg = _cfg_with(
        endpoint="wss://api/cdp",
        auth_kind="bearer",
        auth={"token_env": "MISSING_X"},
        provider_hint="hyperbrowser",
    )
    r = await CloudBackend(cfg).probe()
    assert r.available is False
    assert "auth misconfigured" in r.detail
    assert r.extras["configured"] is False
    assert r.extras["provider"] == "hyperbrowser"  # hint preserved
