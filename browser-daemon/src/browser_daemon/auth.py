"""Auth provider abstraction for the v0.5 `cloud` backend.

Why this exists (spec §8.1.1):

The v0.1 `env` backend covered cloud browsers (Browser Use / Browserless /
Hyperbrowser) by transparently forwarding a user-supplied ws URL — works
fine when the cloud service accepts URL-embedded auth (`?api_key=...` or
`wss://user:pass@host/`). But cloud services that require HTTP-header auth
(`Authorization: Bearer ...` / `X-API-Key: ...`) or mTLS client certs can't
be reached this way: in Mode A the daemon hands the URL to the Skill which
opens the ws and there's no header injection point; in Mode B the daemon
itself opens the upstream ws and needs to know the headers.

`cloud` backend solves this by parameterizing per-backend auth via an
`AuthProvider` Protocol. Three concrete starter implementations cover the
realistic 0.5 surface:

- `BearerTokenAuth` — `Authorization: Bearer <token>` (Browser Use,
  Hyperbrowser-style "API key" services)
- `BasicAuth` — RFC 7617 `Authorization: Basic <base64>` OR fall back to
  URL-embedded `user:pass@` so the v0.1 env-backend behavior stays
  expressible through the same abstraction (no regression for users who
  already had basic-auth URLs working)
- `MtlsAuth` — client cert + key, surfaced as an `ssl.SSLContext` for the
  upstream ws connect path (websockets accepts `ssl=` kwarg)

`OAuth2Auth` is a stub Protocol marker for v0.6 — we expose the type so
callers can `isinstance(provider, OAuth2Auth)` to render an "OAuth flow
coming v0.6" hint in `doctor` output.

Design constraints:

- Auth is **read-only resolution** at this layer. No HTTP requests inside
  `headers()` / `url_with_auth()` — those are pure functions over the
  provider's static config. The OAuth refresh hook is the one exception
  (it can do a token-refresh round-trip), called by the daemon when the
  upstream connect 401s.
- Providers are **synchronous to construct + async to consume**. That
  matches every other backend in the package.
- No magical credential discovery (`AWS_*`, `BROWSER_USE_API_KEY` is
  named in config — there's no `~/.cloud-creds.json` walker). Spec keeps
  the security surface minimal: tokens come from explicit env vars or
  explicit file paths in `config.toml`.
"""
from __future__ import annotations

import base64
import os
import ssl
from dataclasses import dataclass
from pathlib import Path
from typing import Awaitable, Callable, Protocol, runtime_checkable
from urllib.parse import quote, urlparse, urlunparse

from .errors import UserError


# ---- Protocol --------------------------------------------------------------


@runtime_checkable
class AuthProvider(Protocol):
    """Per-cloud-backend auth strategy.

    Mode B upstream ws connect reads `headers()` + `ssl_context()` and
    passes them to `websockets.connect()`. Mode A `url` resolver reads
    `url_with_auth()` to fold any URL-embeddable credential into the
    output URL.
    """

    kind: str
    """Stable identifier — used in doctor output + config validation."""

    async def headers(self) -> dict[str, str]:
        """Headers to add to the upstream ws handshake (Mode B).

        Returns `{}` for auth styles that can't be header-encoded (mTLS,
        URL-embedded). Pure: no I/O unless `refresh()` was just called.
        """
        ...

    async def url_with_auth(self, url: str) -> str:
        """Fold credentials into the URL when possible (basic auth,
        URL-embedded tokens). Returns `url` unchanged for header-based or
        cert-based auth.

        Used by Mode A `browser-daemon url` so Skills that open their own
        ws still get the credential through.
        """
        ...

    def ssl_context(self) -> ssl.SSLContext | None:
        """Build an `ssl.SSLContext` with client cert/key when mTLS is in
        play. Returns None for non-mTLS providers.

        Called once at upstream-open time; not pure (reads cert files).
        """
        ...

    async def refresh(self) -> None:
        """Refresh expired credentials (OAuth2 access tokens, time-bounded
        STS sessions). For static credential styles (bearer / basic /
        mTLS) this is a no-op. Called by the daemon when an upstream
        connect returns HTTP 401.
        """
        ...

    def supports_websocket_auth(self) -> bool:
        """Whether this provider produces something usable for ws-level
        auth. Doctor uses this to print a "yes / no" hint."""
        ...


# ---- BearerTokenAuth -------------------------------------------------------


@dataclass
class BearerTokenAuth:
    """`Authorization: Bearer <token>` header injection.

    Resolution order (highest first):
      1. Explicit `token=` constructor arg (test seam)
      2. Env var named by `token_env` (set in config.toml)
      3. Static `token` field (deprecated but config-supported for one-off
         hard-coded keys — strongly discouraged but not forbidden)

    Surface ambiguity NOTE: cloud providers vary in header name. Browser
    Use accepts `Authorization: Bearer X-API-Key`; Browserless takes
    `?token=` (URL-embedded → use env backend); Hyperbrowser uses
    `X-API-Key`. The `header_name` field lets each cloud-config row tune
    it without forking the class.
    """

    kind: str = "bearer"
    token: str | None = None
    token_env: str | None = None
    header_name: str = "Authorization"
    header_prefix: str = "Bearer "

    def _read_token(self) -> str:
        if self.token is not None:
            return self.token
        if self.token_env:
            v = os.environ.get(self.token_env, "")
            if not v:
                raise UserError(
                    f"BearerTokenAuth: env var {self.token_env!r} is unset "
                    f"or empty")
            return v
        raise UserError(
            "BearerTokenAuth requires either `token` or `token_env` to be set")

    async def headers(self) -> dict[str, str]:
        return {self.header_name: f"{self.header_prefix}{self._read_token()}"}

    async def url_with_auth(self, url: str) -> str:
        return url  # bearer tokens are header-only by convention

    def ssl_context(self) -> ssl.SSLContext | None:
        return None

    async def refresh(self) -> None:
        return None

    def supports_websocket_auth(self) -> bool:
        return True


# ---- BasicAuth -------------------------------------------------------------


@dataclass
class BasicAuth:
    """RFC 7617 basic auth. Two flavors:

    - **header mode** (default, `embed_in_url=False`): emits the
      `Authorization: Basic <base64(user:pass)>` header for Mode B upstream.
      Mode A `url_with_auth()` still falls back to URL-embedded so Skills
      that consume Mode A URLs continue to work — there's no header
      injection point on Mode A.
    - **URL-embedded mode** (`embed_in_url=True`): forces `user:pass@host`
      embedding even in Mode B. Compatible with the v0.1 env-backend
      behavior; useful when the upstream server only accepts basic-auth
      via URL (rare but seen in fingerprint-browser farms).

    Credentials come from:
      - constructor `username`/`password` (test seam)
      - `username_env`/`password_env` (recommended for production)
    """

    kind: str = "basic"
    username: str | None = None
    password: str | None = None
    username_env: str | None = None
    password_env: str | None = None
    embed_in_url: bool = False

    def _resolve(self) -> tuple[str, str]:
        u = self.username
        if u is None and self.username_env:
            u = os.environ.get(self.username_env)
        p = self.password
        if p is None and self.password_env:
            p = os.environ.get(self.password_env)
        if not u or p is None:
            raise UserError(
                "BasicAuth: username + password must be resolvable from either "
                "constructor args or `username_env`/`password_env`")
        return u, p

    async def headers(self) -> dict[str, str]:
        if self.embed_in_url:
            return {}  # credential goes in URL, not header
        u, p = self._resolve()
        token = base64.b64encode(f"{u}:{p}".encode("utf-8")).decode("ascii")
        return {"Authorization": f"Basic {token}"}

    async def url_with_auth(self, url: str) -> str:
        # We embed when Mode A asks OR when embed_in_url forces it.
        # `url_with_auth` callers are Mode-A code paths — header injection
        # isn't available there, so embedding is the only way to surface
        # the credential.
        u, p = self._resolve()
        parsed = urlparse(url)
        if parsed.username or parsed.password:
            return url  # already embedded, don't double-stamp
        # urllib.parse percent-encoding leaves `@` / `:` alone — manual:
        creds = f"{quote(u, safe='')}:{quote(p, safe='')}"
        netloc = f"{creds}@{parsed.hostname or ''}"
        if parsed.port:
            netloc += f":{parsed.port}"
        return urlunparse((
            parsed.scheme, netloc, parsed.path, parsed.params,
            parsed.query, parsed.fragment,
        ))

    def ssl_context(self) -> ssl.SSLContext | None:
        return None

    async def refresh(self) -> None:
        return None

    def supports_websocket_auth(self) -> bool:
        return True


# ---- MtlsAuth --------------------------------------------------------------


@dataclass
class MtlsAuth:
    """Mutual TLS via client certificate + private key.

    Both files must be PEM-encoded. The optional `ca_file` lets the user
    pin a custom CA (e.g., self-signed cloud-provider CA). The optional
    `key_password_env` covers encrypted keys without putting the password
    in config.toml.

    Surface: `ssl_context()` is the only meaningful method here —
    `headers()` returns `{}` because mTLS authenticates at the TLS layer
    below HTTP/ws. The CDP transport layer (`server/upstream.py`) passes
    the SSLContext to `websockets.connect(ssl=...)`.
    """

    kind: str = "mtls"
    cert_file: str = ""
    key_file: str = ""
    ca_file: str | None = None
    key_password_env: str | None = None

    def _resolve_password(self) -> str | None:
        if not self.key_password_env:
            return None
        v = os.environ.get(self.key_password_env, "")
        return v or None

    async def headers(self) -> dict[str, str]:
        return {}

    async def url_with_auth(self, url: str) -> str:
        return url

    def ssl_context(self) -> ssl.SSLContext | None:
        if not self.cert_file or not self.key_file:
            raise UserError(
                "MtlsAuth: cert_file + key_file are required")
        cert_path = Path(self.cert_file).expanduser()
        key_path = Path(self.key_file).expanduser()
        if not cert_path.is_file():
            raise UserError(f"MtlsAuth: cert_file does not exist: {cert_path}")
        if not key_path.is_file():
            raise UserError(f"MtlsAuth: key_file does not exist: {key_path}")
        ctx = ssl.create_default_context(ssl.Purpose.SERVER_AUTH)
        pwd = self._resolve_password()
        ctx.load_cert_chain(str(cert_path), str(key_path), password=pwd)
        if self.ca_file:
            ca_path = Path(self.ca_file).expanduser()
            if not ca_path.is_file():
                raise UserError(f"MtlsAuth: ca_file does not exist: {ca_path}")
            ctx.load_verify_locations(str(ca_path))
        return ctx

    async def refresh(self) -> None:
        return None

    def supports_websocket_auth(self) -> bool:
        return True


# ---- OAuth2Auth (v0.6 stub) ------------------------------------------------


@dataclass
class OAuth2Auth:
    """Placeholder for OAuth2 access-token-with-refresh flows. v0.6 will
    fill this in (token cache file + refresh round-trip). v0.5 surfaces
    the type so doctor / install wizard can render a "coming v0.6" row
    without conditional imports.

    Calling any method raises `UserError("not implemented in v0.5")`.
    """

    kind: str = "oauth2"
    issuer_url: str = ""
    client_id: str = ""
    client_secret_env: str | None = None
    refresh_token_file: str | None = None
    scopes: tuple[str, ...] = ()

    def _not_yet(self) -> "UserError":
        return UserError(
            "OAuth2Auth is a v0.6 placeholder; please use BearerTokenAuth "
            "with a manually-obtained access token until v0.6 ships")

    async def headers(self) -> dict[str, str]:
        raise self._not_yet()

    async def url_with_auth(self, url: str) -> str:
        raise self._not_yet()

    def ssl_context(self) -> ssl.SSLContext | None:
        return None

    async def refresh(self) -> None:
        raise self._not_yet()

    def supports_websocket_auth(self) -> bool:
        return False


# ---- factory ---------------------------------------------------------------


def build_auth_provider(
    auth_kind: str, config: dict,
) -> AuthProvider:
    """Build the provider from a `[backends.cloud.auth.<kind>]` config dict.

    Centralized factory keeps the cloud-backend module from depending on
    every concrete class directly — it just dispatches on the string kind.
    Unknown kinds raise UserError (caught by the resolver → exit code 1).
    """
    config = config or {}
    if auth_kind == "bearer":
        return BearerTokenAuth(
            token=config.get("token"),
            token_env=config.get("token_env"),
            header_name=config.get("header_name", "Authorization"),
            header_prefix=config.get("header_prefix", "Bearer "),
        )
    if auth_kind == "basic":
        return BasicAuth(
            username=config.get("username"),
            password=config.get("password"),
            username_env=config.get("username_env"),
            password_env=config.get("password_env"),
            embed_in_url=bool(config.get("embed_in_url", False)),
        )
    if auth_kind == "mtls":
        return MtlsAuth(
            cert_file=config.get("cert_file", ""),
            key_file=config.get("key_file", ""),
            ca_file=config.get("ca_file"),
            key_password_env=config.get("key_password_env"),
        )
    if auth_kind == "oauth2":
        return OAuth2Auth(
            issuer_url=config.get("issuer_url", ""),
            client_id=config.get("client_id", ""),
            client_secret_env=config.get("client_secret_env"),
            refresh_token_file=config.get("refresh_token_file"),
            scopes=tuple(config.get("scopes", ()) or ()),
        )
    raise UserError(
        f"unknown auth_kind {auth_kind!r}; supported: bearer, basic, mtls, oauth2")
