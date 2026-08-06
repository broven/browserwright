"""`CdpBackend` URL resolution — the merged rdp+env backend (#38).

The env half of this had **no unit test at all** before the merge: neither the
verbatim ws pass-through nor the http `/json/version` discovery was covered
anywhere. That is also why the `trust_env` and 404-fallback differences between
the two old backends could sit encoded in their *names* unchallenged.
"""
from __future__ import annotations

import httpx
import pytest

from browserwright.daemon.backends.cdp import CdpBackend
from browserwright.daemon.config import CdpConfig, BackendsConfig, Config
from browserwright.daemon.errors import Unavailable


def _cfg(*, port: int = 9222, endpoint: str | None = None) -> Config:
    return Config(backend="cdp", backends=BackendsConfig(
        cdp=CdpConfig(port=port, endpoint=endpoint)))


def _no_http(monkeypatch):
    """Make any HTTP client construction fail the test."""
    def _boom(*a, **kw):
        pytest.fail("resolve opened an HTTP client when it should not have")
    monkeypatch.setattr(httpx, "AsyncClient", _boom)


class _Resp:
    def __init__(self, status_code, payload=None):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        if self._payload is None:
            raise ValueError("not json")
        return self._payload


# ---- ws endpoints: verbatim, no network ------------------------------------


@pytest.mark.asyncio
async def test_ws_endpoint_is_returned_untouched(monkeypatch):
    _no_http(monkeypatch)
    url = "ws://box.local:9222/devtools/browser/2f1c"

    ws, extras = await CdpBackend(_cfg(endpoint=url))._resolve_source(1.0)

    assert ws == url
    assert "discovery_url" not in extras


@pytest.mark.asyncio
async def test_token_bearing_wss_survives_byte_for_byte(monkeypatch):
    """Guards against a future well-meaning normaliser.

    Userinfo and query are where cloud providers put credentials; rewriting the
    URL at all — even just re-encoding it — can invalidate the token.
    """
    _no_http(monkeypatch)
    url = "wss://user:s3cr3t@cloud.example.com:443/cdp?apiKey=deadbeef&x=1"

    ws, _ = await CdpBackend(_cfg(endpoint=url))._resolve_source(1.0)

    assert ws == url


@pytest.mark.asyncio
async def test_unsupported_scheme_is_refused(monkeypatch):
    _no_http(monkeypatch)

    with pytest.raises(Unavailable, match="scheme"):
        await CdpBackend(_cfg(endpoint="ftp://host/x"))._resolve_source(1.0)


# ---- http endpoints: /json/version discovery -------------------------------


@pytest.mark.asyncio
async def test_http_endpoint_goes_through_json_version(monkeypatch):
    seen = []

    async def fake_get(self, url, timeout):
        seen.append(url)
        return _Resp(200, {"webSocketDebuggerUrl": "ws://remote:9222/devtools/browser/x"})

    monkeypatch.setattr(CdpBackend, "_get", fake_get)

    ws, extras = await CdpBackend(
        _cfg(endpoint="http://remote.example:9222"))._resolve_source(1.0)

    assert seen == ["http://remote.example:9222/json/version"]
    assert ws == "ws://remote:9222/devtools/browser/x"
    assert extras["discovery_url"] == "http://remote.example:9222"


# ---- the 404 fallback is local-only ----------------------------------------


@pytest.mark.asyncio
async def test_remote_404_does_not_consult_devtools_active_port(monkeypatch):
    """A 404 from someone else's host is just a 404.

    `DevToolsActivePort` is a file on *this* machine's disk, so it can only
    ever speak for a local browser. The old `env` backend got this right by
    accident (it never had the fallback at all); the merged backend has to get
    it right on purpose.
    """
    async def fake_get(self, url, timeout):
        return _Resp(404)

    monkeypatch.setattr(CdpBackend, "_get", fake_get)
    monkeypatch.setattr(
        "browserwright.daemon.backends.cdp._ws_from_devtools_active_port",
        lambda url: pytest.fail("consulted a local file for a remote browser"))

    with pytest.raises(Unavailable, match="404"):
        await CdpBackend(
            _cfg(endpoint="http://remote.example:9222"))._resolve_source(1.0)


@pytest.mark.asyncio
async def test_loopback_404_does_consult_devtools_active_port(monkeypatch):
    """...and a loopback URL now gets the fallback the old env path denied it.

    `--attach=http://127.0.0.1:9222` was refused the Chrome 136+ lockdown
    workaround purely because the backend serving it was called `env`.
    """
    async def fake_get(self, url, timeout):
        return _Resp(404)

    monkeypatch.setattr(CdpBackend, "_get", fake_get)
    monkeypatch.setattr(
        "browserwright.daemon.backends.cdp._ws_from_devtools_active_port",
        lambda url: "ws://127.0.0.1:9222/devtools/browser/from-file")
    monkeypatch.setattr(
        "browserwright.daemon.backends.cdp._find_matching_profile",
        lambda port: None)

    ws, extras = await CdpBackend(
        _cfg(endpoint="http://127.0.0.1:9222"))._resolve_source(1.0)

    assert ws == "ws://127.0.0.1:9222/devtools/browser/from-file"
    assert extras["isolated_profile"] is False


@pytest.mark.asyncio
async def test_fallback_uses_the_endpoint_port_not_the_config_default(monkeypatch):
    """The port to match is the one in the URL, not `backends.cdp.port`.

    Easy bug to write: reach for `self.port` (9222 by default) while resolving
    an endpoint that names 9444, and silently match the wrong profile.
    """
    seen = []

    async def fake_get(self, url, timeout):
        return _Resp(404)

    monkeypatch.setattr(CdpBackend, "_get", fake_get)
    monkeypatch.setattr(
        "browserwright.daemon.backends.cdp._ws_from_devtools_active_port",
        lambda url: "ws://127.0.0.1:9444/devtools/browser/x")
    monkeypatch.setattr(
        "browserwright.daemon.backends.cdp._find_matching_profile",
        lambda port: seen.append(port) or None)

    await CdpBackend(
        _cfg(port=9222, endpoint="http://127.0.0.1:9444"))._resolve_source(1.0)

    assert seen == [9444]


# ---- trust_env is derived from the endpoint, not from a name ---------------


@pytest.mark.parametrize(("endpoint", "trusts_proxy"), [
    (None, False),                                   # port mode: always local
    ("ws://127.0.0.1:9222/devtools/browser/x", False),
    ("ws://[::1]:9222/devtools/browser/x", False),
    ("http://localhost:9222", False),
    ("http://127.0.0.2:9222", False),                # the whole 127/8 range
    ("wss://cloud.example.com/cdp", True),
    ("http://192.168.1.10:9222", True),
])
def test_proxy_trust_follows_the_endpoint(endpoint, trusts_proxy):
    """One predicate replaced two name checks.

    `rdp` hard-coded `trust_env=False` and `env` hard-coded `True`. Both were
    answering the same question — is this browser on my machine? — so the merged
    backend asks it directly.
    """
    assert CdpBackend(_cfg(endpoint=endpoint))._trust_env is trusts_proxy


# ---- the deleted env vars are inert ----------------------------------------


@pytest.mark.parametrize("var", [
    "BD_CDP_WS", "BU_CDP_WS", "BD_CDP_URL", "BU_CDP_URL",
])
def test_deleted_env_vars_do_not_reach_the_endpoint(monkeypatch, var):
    """Proves they are ignored, not quietly still honoured.

    Deleting the read is the easy half; this is the half that catches someone
    re-adding it, or a stale value in a developer's shell changing a result.
    """
    from browserwright.daemon.config import load

    monkeypatch.setenv(var, "ws://should-be-ignored.example/cdp")
    cfg = load()

    assert cfg.backends.cdp.endpoint is None
    assert not hasattr(cfg, "cdp_ws")
    assert not hasattr(cfg, "cdp_url")


# ---- errors do not leak the token ------------------------------------------


@pytest.mark.asyncio
async def test_resolve_errors_redact_the_endpoint(monkeypatch):
    """`Unavailable` reaches the client *and* the daemon log."""
    async def fake_get(self, url, timeout):
        raise httpx.ConnectError("refused")

    monkeypatch.setattr(CdpBackend, "_get", fake_get)
    url = "https://user:s3cr3t@cloud.example.com/cdp?apiKey=deadbeef"

    with pytest.raises(Unavailable) as exc:
        await CdpBackend(_cfg(endpoint=url))._resolve_source(1.0)

    blob = str(exc.value) + repr(getattr(exc.value, "attempts", None))
    assert "s3cr3t" not in blob
    assert "deadbeef" not in blob
    assert "cloud.example.com" in blob  # still identifies the endpoint
