"""env backend — caller injects the ws URL out-of-band.

Spec §8.1 + §8.1.1: BD_CDP_WS is trusted verbatim (no parsing, no rewriting —
that's the whole point of the env path for cloud / fingerprint browsers with
URL-embedded tokens). BD_CDP_URL goes through `/json/version` to pull the
webSocketDebuggerUrl field, same as rdp but pointed at an arbitrary host.

BU_CDP_WS / BU_CDP_URL are compat aliases honored at the Config layer
(config.py records `cdp_ws_source`); doctor surfaces a "please migrate" hint
when those fired.
"""
from __future__ import annotations

import httpx

from ..config import Config
from ..errors import Unavailable
from .base import Backend, DoctorResult, ResolveResult


class EnvBackend(Backend):
    name = "env"
    kind = "UPSTREAM_WS"
    recommended_mode: str = "A"
    ux_cost = "none"

    def __init__(self, cfg: Config):
        self._cfg = cfg

    # ------------------------------------------------------------------ probe

    async def probe(self) -> DoctorResult:
        cfg = self._cfg
        if cfg.cdp_ws:
            return DoctorResult(
                name=self.name,
                available=True,
                ws_url=None,  # doctor never opens a ws — spec §5.2 contract
                detail=self._origin_detail("BD_CDP_WS" if cfg.cdp_ws_source == "BD_CDP_WS"
                                          else "BU_CDP_WS"),
                ux_cost=self.ux_cost,
            )
        if cfg.cdp_url:
            return DoctorResult(
                name=self.name,
                available=True,
                ws_url=None,
                detail=self._origin_detail("BD_CDP_URL" if cfg.cdp_url_source == "BD_CDP_URL"
                                           else "BU_CDP_URL"),
                ux_cost=self.ux_cost,
            )
        return DoctorResult(
            name=self.name,
            available=False,
            ws_url=None,
            detail="BD_CDP_WS not set, BD_CDP_URL not set",
            ux_cost=self.ux_cost,
        )

    def _origin_detail(self, source: str) -> str:
        if source.startswith("BU_"):
            return f"{source} set (deprecated, please rename to BD_{source[3:]})"
        return f"{source} set"

    # ---------------------------------------------------------------- resolve

    async def resolve(self, timeout: float) -> ResolveResult:
        cfg = self._cfg
        if cfg.cdp_ws:
            return ResolveResult(ws_url=cfg.cdp_ws, backend=self.name, extras={})
        if cfg.cdp_url:
            ws = await _resolve_via_json_version(cfg.cdp_url, timeout)
            return ResolveResult(
                ws_url=ws,
                backend=self.name,
                extras={"discovery_url": cfg.cdp_url},
            )
        raise Unavailable(
            "env backend: neither BD_CDP_WS nor BD_CDP_URL is set",
            attempts={self.name: "BD_CDP_WS not set, BD_CDP_URL not set"},
        )


async def _resolve_via_json_version(base_url: str, timeout: float) -> str:
    """HTTP GET {base_url}/json/version and return webSocketDebuggerUrl.

    Spec §8.1: this is the standard CDP HTTP discovery shape that all real and
    fingerprint browsers, plus most cloud providers, agree on. We keep this
    function shared with `rdp`-style probes (re-imported there) so the two
    paths can never diverge on edge handling (timeout / non-200 / missing key).
    """
    url = f"{base_url.rstrip('/')}/json/version"
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.get(url)
    except (httpx.HTTPError, OSError) as e:
        raise Unavailable(
            f"env backend: HTTP discovery failed for {url}: {e}",
            attempts={"env": f"GET {url} -> {type(e).__name__}: {e}"},
        ) from e
    if resp.status_code != 200:
        raise Unavailable(
            f"env backend: {url} returned HTTP {resp.status_code}",
            attempts={"env": f"GET {url} -> HTTP {resp.status_code}"},
        )
    try:
        body = resp.json()
    except ValueError as e:
        raise Unavailable(
            f"env backend: {url} returned non-JSON body: {e}",
            attempts={"env": f"GET {url} -> non-JSON"},
        ) from e
    ws = body.get("webSocketDebuggerUrl") if isinstance(body, dict) else None
    if not isinstance(ws, str) or not ws:
        raise Unavailable(
            f"env backend: {url} JSON has no webSocketDebuggerUrl field",
            attempts={"env": f"GET {url} -> missing webSocketDebuggerUrl"},
        )
    return ws
