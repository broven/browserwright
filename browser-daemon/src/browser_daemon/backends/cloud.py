"""cloud backend — v0.5 (spec §7 + §8.1.1).

The cloud backend exists to cover what the v0.1 `env` backend couldn't:
HTTP-header auth and mTLS for remote browser services (Browser Use,
Browserless, Hyperbrowser, etc.). It still produces an upstream CDP ws
URL (so `kind=UPSTREAM_WS`, not LOCAL_RELAY) — the difference is that
the daemon's Mode B upstream-ws connect needs the auth provider's
headers / ssl context to authenticate the handshake. Mode A is degraded
but supported: for URL-embeddable auth styles (basic, URL-token), we
fold credentials into the output URL; for header / mTLS auth, Mode A
emits a warning that the resulting URL won't authenticate (the user
needs Mode B serve to actually connect).

Endpoint resolution:
  - `wss://...` / `ws://...` → used as-is
  - `https://...` / `http://...` → HTTP GET `{endpoint}/json/version`
    and read `webSocketDebuggerUrl` (same shape as env backend's
    BD_CDP_URL path). For Authorization-header auth, the discovery GET
    also carries the header — otherwise some services 401 the GET.
"""
from __future__ import annotations

import logging
from typing import Any

import httpx

from ..auth import AuthProvider, build_auth_provider
from ..config import Config
from ..errors import Unavailable, UserError
from .base import Backend, DoctorResult, ResolveResult

logger = logging.getLogger(__name__)


class CloudBackend(Backend):
    name = "cloud"
    kind = "UPSTREAM_WS"
    recommended_mode: str = "B"  # header / mTLS auth needs Mode B to inject
    ux_cost = "auth-required"  # v0.5 — new enum value, doctor contract

    def __init__(self, cfg: Config):
        self._cfg = cfg
        self._cloud = cfg.backends.cloud

    # ---- internal: build the AuthProvider (cached per-instance) ---------

    _provider_cache: AuthProvider | None = None

    def _provider(self) -> AuthProvider | None:
        if self._provider_cache is not None:
            return self._provider_cache
        if not self._cloud.auth_kind:
            return None
        try:
            p = build_auth_provider(self._cloud.auth_kind, self._cloud.auth)
        except UserError:
            raise
        self._provider_cache = p
        return p

    # ---- probe ----------------------------------------------------------

    def _extras(self, *, configured: bool) -> dict:
        """Doctor JSON `extras` contract (v0.5) for cloud backend. Matches
        skill-impl-2's `_extension_backend_available()` mirror pattern —
        skill reads `entry["extras"]["configured"]` in install wizard.

        Schema (stable, additions are minor backend bumps not schema-level):
          provider:   str (browser-use | browserless | hyperbrowser | generic)
          endpoint:   str | None  (configured ws / https URL, or None)
          auth_kind:  str | None  (bearer | basic | mtls | oauth2 | None)
          configured: bool  (True iff endpoint + auth_kind both present
                             AND auth provider loads without raising)
        """
        return {
            "provider": self._cloud.provider_hint or "generic",
            "endpoint": self._cloud.endpoint,
            "auth_kind": self._cloud.auth_kind,
            "configured": configured,
        }

    async def probe(self) -> DoctorResult:
        """Doctor probe — never opens a ws and **never** TCP-connects to the
        cloud endpoint (spec §H3 / §5.2: zero side effects).

        Contract (v0.5):
          - `available=true` iff cloud config is complete AND auth provider
            loads cleanly. We do NOT round-trip to the cloud server — that
            would mean a TLS handshake (TCP connect + cert exchange) which
            is a network side effect doctor must not have.
          - `available=false + detail="not configured; ..."` when no endpoint
            is configured.
          - `available=false + detail="auth misconfigured: ..."` when auth
            provider construction or eval fails (bad token_env, missing
            cert file).
          - `ux_cost="auth-required"` always (new enum value v0.5).
          - `extras` carries `{provider, endpoint, auth_kind, configured}`.
        """
        ep = self._cloud.endpoint
        if not ep:
            return DoctorResult(
                name=self.name,
                available=False,
                ws_url=None,
                detail="not configured; configure under [backends.cloud] in config.toml",
                needs_user_action=(
                    "configure `[backends.cloud]` in config.toml — "
                    "see README §cloud backend"),
                ux_cost=self.ux_cost,
                extras=self._extras(configured=False),
            )
        try:
            provider = self._provider()
        except UserError as e:
            return DoctorResult(
                name=self.name, available=False, ws_url=None,
                detail=f"auth misconfigured: {e}",
                needs_user_action="fix [backends.cloud.auth.*] in config.toml",
                ux_cost=self.ux_cost,
                extras=self._extras(configured=False),
            )

        # Validate the AuthProvider can produce headers / ssl context. This
        # touches local config (env vars, cert files) but does NOT do any
        # network I/O — preserves the zero-side-effect doctor contract.
        try:
            if provider is not None:
                _ = await provider.headers()
                # ssl_context() reads cert files; raises UserError on bad path.
                _ = provider.ssl_context()
        except UserError as e:
            return DoctorResult(
                name=self.name, available=False, ws_url=None,
                detail=f"auth misconfigured: {e}",
                needs_user_action="check env vars / cert files",
                ux_cost=self.ux_cost,
                extras=self._extras(configured=False),
            )

        auth_kind = self._cloud.auth_kind or "(none)"
        hint = self._cloud.provider_hint or "generic"
        return DoctorResult(
            name=self.name,
            available=True,
            ws_url=None,
            detail=f"provider={hint} (auth_kind={auth_kind}, endpoint={ep})",
            ux_cost=self.ux_cost,
            extras=self._extras(configured=True),
        )

    # ---- resolve --------------------------------------------------------

    async def resolve(self, timeout: float) -> ResolveResult:
        ep = self._cloud.endpoint
        if not ep:
            raise Unavailable(
                "cloud backend has no endpoint configured",
                attempts={self.name: "no endpoint"},
            )
        provider = self._provider()
        # Pre-baked direct ws URL — just fold URL-embeddable auth in.
        if ep.startswith(("ws://", "wss://")):
            url = ep
            if provider is not None:
                url = await provider.url_with_auth(url)
            extras: dict[str, Any] = {
                "auth_kind": self._cloud.auth_kind or "none",
                "provider": self._cloud.provider_hint or "generic",
            }
            if provider is not None and not provider.supports_websocket_auth():
                # OAuth2 stub etc. — surface the limitation explicitly.
                extras["warning"] = (
                    f"auth_kind={self._cloud.auth_kind} doesn't support "
                    "websocket auth in v0.5; Mode A URL will not "
                    "authenticate by itself")
            return ResolveResult(ws_url=url, backend=self.name, extras=extras)

        # HTTP endpoint → discover via /json/version (matching env backend's
        # BD_CDP_URL shape).
        try:
            headers = await provider.headers() if provider is not None else {}
        except UserError as e:
            raise Unavailable(
                f"cloud backend auth headers unresolvable: {e}",
                attempts={self.name: str(e)},
            )
        url = ep.rstrip("/") + "/json/version"
        try:
            async with httpx.AsyncClient(
                    timeout=timeout, trust_env=False, mounts={}) as client:
                resp = await client.get(url, headers=headers)
        except (httpx.HTTPError, OSError) as e:
            raise Unavailable(
                f"cloud backend discovery failed: {e}",
                attempts={self.name: f"{type(e).__name__}: {e}"},
            )
        if resp.status_code != 200:
            raise Unavailable(
                f"cloud backend discovery returned HTTP {resp.status_code}",
                attempts={self.name: f"HTTP {resp.status_code} from {url}"},
            )
        try:
            body = resp.json()
        except ValueError:
            body = {}
        ws = body.get("webSocketDebuggerUrl") if isinstance(body, dict) else None
        if not isinstance(ws, str) or not ws:
            raise Unavailable(
                f"cloud backend /json/version has no webSocketDebuggerUrl",
                attempts={self.name: f"response body: {body!r}"},
            )
        if provider is not None:
            ws = await provider.url_with_auth(ws)
        return ResolveResult(
            ws_url=ws,
            backend=self.name,
            extras={
                "auth_kind": self._cloud.auth_kind or "none",
                "provider": self._cloud.provider_hint or "generic",
            },
        )
