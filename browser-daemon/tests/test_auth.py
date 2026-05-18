"""AuthProvider unit tests (v0.5).

Exercises every concrete provider's `headers()` / `url_with_auth()` /
`ssl_context()` paths, the factory dispatch, and the negative cases that
should raise UserError (missing env, bad files, unknown kind, OAuth2
stubs).
"""
from __future__ import annotations

import asyncio
import base64
import ssl
import tempfile
from pathlib import Path

import pytest

from browser_daemon.auth import (
    AuthProvider, BearerTokenAuth, BasicAuth, MtlsAuth, OAuth2Auth,
    build_auth_provider,
)
from browser_daemon.errors import UserError


# ---- BearerTokenAuth ------------------------------------------------------


@pytest.mark.asyncio
async def test_bearer_explicit_token_emits_authorization_header():
    a = BearerTokenAuth(token="t0p-s3cr3t")
    h = await a.headers()
    assert h == {"Authorization": "Bearer t0p-s3cr3t"}
    # URL passthrough: bearer is header-only.
    assert await a.url_with_auth("wss://x/") == "wss://x/"
    assert a.ssl_context() is None
    assert a.supports_websocket_auth() is True


@pytest.mark.asyncio
async def test_bearer_token_env_reads_environment(monkeypatch):
    monkeypatch.setenv("ABC_KEY", "from-env-99")
    a = BearerTokenAuth(token_env="ABC_KEY")
    h = await a.headers()
    assert h["Authorization"] == "Bearer from-env-99"


@pytest.mark.asyncio
async def test_bearer_missing_env_raises_user_error(monkeypatch):
    monkeypatch.delenv("MISSING_KEY", raising=False)
    a = BearerTokenAuth(token_env="MISSING_KEY")
    with pytest.raises(UserError) as exc:
        await a.headers()
    assert "MISSING_KEY" in str(exc.value)


@pytest.mark.asyncio
async def test_bearer_neither_token_nor_env_raises():
    a = BearerTokenAuth()
    with pytest.raises(UserError):
        await a.headers()


@pytest.mark.asyncio
async def test_bearer_custom_header_name_and_prefix():
    """Some clouds use `X-API-Key` (Hyperbrowser) — no `Bearer` prefix.
    Header name + prefix must be tunable."""
    a = BearerTokenAuth(token="abc", header_name="X-API-Key", header_prefix="")
    h = await a.headers()
    assert h == {"X-API-Key": "abc"}


# ---- BasicAuth ------------------------------------------------------------


@pytest.mark.asyncio
async def test_basic_default_header_mode_emits_b64_authorization():
    a = BasicAuth(username="u", password="p")
    h = await a.headers()
    expected = base64.b64encode(b"u:p").decode()
    assert h == {"Authorization": f"Basic {expected}"}


@pytest.mark.asyncio
async def test_basic_url_embed_via_url_with_auth_mode_a_path():
    """Mode A code paths invoke `url_with_auth` — no header injection
    point exists for Skills that open ws themselves, so basic auth has to
    embed into the URL even when default mode is header-only."""
    a = BasicAuth(username="alice", password="se cret")  # space needs encoding
    out = await a.url_with_auth("wss://api.example.com/cdp")
    assert out.startswith("wss://alice:se%20cret@api.example.com/cdp")


@pytest.mark.asyncio
async def test_basic_url_with_auth_preserves_existing_creds():
    """Don't double-stamp if the URL already has user:pass@."""
    a = BasicAuth(username="me", password="x")
    out = await a.url_with_auth("wss://other:pw@host/p")
    assert "me:" not in out


@pytest.mark.asyncio
async def test_basic_embed_in_url_flag_no_header():
    a = BasicAuth(username="u", password="p", embed_in_url=True)
    assert await a.headers() == {}  # header path opts out


@pytest.mark.asyncio
async def test_basic_env_resolution(monkeypatch):
    monkeypatch.setenv("BU", "bob")
    monkeypatch.setenv("BP", "rope")
    a = BasicAuth(username_env="BU", password_env="BP")
    h = await a.headers()
    expected = base64.b64encode(b"bob:rope").decode()
    assert h["Authorization"] == f"Basic {expected}"


@pytest.mark.asyncio
async def test_basic_missing_creds_raises():
    a = BasicAuth(username_env="NOPE_U", password_env="NOPE_P")
    with pytest.raises(UserError):
        await a.headers()


# ---- MtlsAuth -------------------------------------------------------------


def _make_self_signed_cert(tmp_path: Path) -> tuple[Path, Path]:
    """Generate a self-signed cert + key in PEM format for testing.

    We do this with the `ssl` stdlib's underlying OpenSSL via `subprocess`
    because Python's stdlib doesn't expose cert-generation directly and we
    don't want to pull in `cryptography` just for tests. Falls back to
    pre-baked PEM if openssl isn't available.
    """
    import subprocess
    import shutil
    cert = tmp_path / "client.crt"
    key = tmp_path / "client.key"
    if shutil.which("openssl"):
        subprocess.run(
            ["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
             "-keyout", str(key), "-out", str(cert),
             "-days", "1", "-subj", "/CN=test-client"],
            check=True, capture_output=True,
        )
        return cert, key
    # Fallback: dummy bytes that load_cert_chain will reject — used to
    # verify the "file exists but can't parse" path differently.
    pytest.skip("openssl not available; mTLS cert generation skipped")


def test_mtls_loads_client_cert_chain_into_ssl_context(tmp_path):
    cert, key = _make_self_signed_cert(tmp_path)
    a = MtlsAuth(cert_file=str(cert), key_file=str(key))
    ctx = a.ssl_context()
    assert isinstance(ctx, ssl.SSLContext)
    # Sanity: ssl context defaults to TLS client, not server.
    assert ctx.verify_mode == ssl.CERT_REQUIRED  # default for create_default_context


@pytest.mark.asyncio
async def test_mtls_headers_empty_url_unchanged(tmp_path):
    cert, key = _make_self_signed_cert(tmp_path)
    a = MtlsAuth(cert_file=str(cert), key_file=str(key))
    assert await a.headers() == {}
    assert await a.url_with_auth("wss://x/") == "wss://x/"


def test_mtls_missing_files_raise_user_error(tmp_path):
    a = MtlsAuth(cert_file=str(tmp_path / "nope.crt"),
                 key_file=str(tmp_path / "nope.key"))
    with pytest.raises(UserError) as exc:
        a.ssl_context()
    assert "does not exist" in str(exc.value)


def test_mtls_requires_cert_and_key_paths():
    a = MtlsAuth()  # no paths set
    with pytest.raises(UserError) as exc:
        a.ssl_context()
    assert "cert_file" in str(exc.value)


# ---- OAuth2Auth (v0.6 stub) -----------------------------------------------


@pytest.mark.asyncio
async def test_oauth2_stub_raises_user_error_everywhere():
    a = OAuth2Auth(issuer_url="https://idp/", client_id="x")
    with pytest.raises(UserError):
        await a.headers()
    with pytest.raises(UserError):
        await a.url_with_auth("wss://x/")
    with pytest.raises(UserError):
        await a.refresh()
    # ssl_context is allowed to be None (no cert path involved); the
    # placeholder shouldn't blow up here.
    assert a.ssl_context() is None
    assert a.supports_websocket_auth() is False  # "doesn't work yet"


# ---- factory dispatch -----------------------------------------------------


def test_factory_builds_bearer():
    a = build_auth_provider("bearer", {"token": "abc"})
    assert isinstance(a, BearerTokenAuth)
    assert a.token == "abc"


def test_factory_builds_basic_with_embed_flag():
    a = build_auth_provider("basic", {
        "username": "u", "password": "p", "embed_in_url": True,
    })
    assert isinstance(a, BasicAuth)
    assert a.embed_in_url is True


def test_factory_builds_mtls():
    a = build_auth_provider("mtls", {
        "cert_file": "/tmp/c", "key_file": "/tmp/k", "ca_file": "/tmp/ca",
    })
    assert isinstance(a, MtlsAuth)
    assert a.ca_file == "/tmp/ca"


def test_factory_builds_oauth2_stub():
    a = build_auth_provider("oauth2", {
        "issuer_url": "https://idp/", "client_id": "C",
    })
    assert isinstance(a, OAuth2Auth)


def test_factory_unknown_kind_raises():
    with pytest.raises(UserError) as exc:
        build_auth_provider("hmac", {})
    assert "unknown auth_kind" in str(exc.value)


# ---- Protocol structural-check ---------------------------------------------


def test_concrete_providers_satisfy_auth_provider_protocol():
    """`@runtime_checkable Protocol` lets us isinstance-check structural
    conformance. Belt-and-suspenders: if someone removes a method from a
    provider class, this test breaks."""
    assert isinstance(BearerTokenAuth(token="x"), AuthProvider)
    assert isinstance(BasicAuth(username="u", password="p"), AuthProvider)
    assert isinstance(MtlsAuth(cert_file="x", key_file="y"), AuthProvider)
    assert isinstance(OAuth2Auth(issuer_url="x", client_id="y"), AuthProvider)
