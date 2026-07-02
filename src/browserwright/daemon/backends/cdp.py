"""Real-CDP backends — ONE implementation, two URL sources.

`rdp` and `env` are the same conceptual backend: real browser-level CDP over a
ws URL, `kind=UPSTREAM_WS`, handled identically downstream (the Router treats
both as a raw-CDP upstream). They differ ONLY in how the ws URL is acquired,
so `RealCdpBackend` carries everything shared — the class attributes, the
DoctorResult/ResolveResult plumbing, and the single `/json/version` discovery
(`_discover_ws_url`) — and each backend id is a thin URL-source subclass:

- `rdp` (spec §8.2): Chrome launched with `--remote-debugging-port=NNNN`.
  Discovery is `/json/version` on `127.0.0.1:port`. The interesting part is
  the Chrome 136/147+ default-profile lockdown: those builds disable HTTP
  discovery when the user-data-dir is the *real* user profile (privacy
  hardening), returning a 404. The websocket path still works, and Chrome
  still writes it into `DevToolsActivePort`. So the fallback is: HTTP 404 →
  walk PROFILES, match the port number on line 1, read the ws path from
  line 2. This mirrors browser-harness `daemon.py:83-101`
  `_ws_from_devtools_active_port` — which has already eaten the
  IPv6-host-bracket and stale-port edges.

- `env` (spec §8.1 + §8.1.1): caller injects the ws URL out-of-band.
  BD_CDP_WS is trusted verbatim (no parsing, no rewriting — that's the whole
  point of the env path for cloud / fingerprint browsers with URL-embedded
  tokens). BD_CDP_URL goes through `/json/version` — the same shared
  discovery as rdp — pointed at an arbitrary host. BU_CDP_WS / BU_CDP_URL are
  compat aliases honored at the Config layer (config.py records
  `cdp_ws_source`); doctor surfaces a "please migrate" hint when those fired.
"""
from __future__ import annotations

from pathlib import Path
from urllib.parse import urlparse

import httpx

from ..config import Config
from ..errors import Unavailable
from ..platforms import profile_paths
from .base import DoctorResult, ResolveResult


class _JsonVersion404(Unavailable):
    """`/json/version` answered HTTP 404 — the Chrome 136/147+ default-profile
    lockdown signature. Raised by `_discover_ws_url` so a URL source can catch
    it and try a fallback (rdp does); uncaught, it is a plain Unavailable."""


class RealCdpBackend:
    """Shared real-CDP backend. Subclasses provide only the URL source:

    - `_probe_source() -> (available, detail)` — cheap, per spec §5.2: never
      opens a ws, never produces ws_url.
    - `_resolve_source(timeout) -> (ws_url, extras)` — HTTP discovery /
      filesystem reads only; raises `Unavailable` on failure.
    """

    name: str
    kind = "UPSTREAM_WS"
    recommended_mode: str = "A"
    ux_cost = "none"
    # rdp overrides to False: the user's HTTP(S)_PROXY / ALL_PROXY must not be
    # applied to localhost probes — proxying to your own loopback is never what
    # anyone wants and triggers httpx[socks] import errors when
    # ALL_PROXY=socks5://... env keeps the default: its target may be a remote
    # host where the proxy is intentional.
    _trust_env: bool = True

    def __init__(self, cfg: Config):
        self._cfg = cfg

    async def probe(self) -> DoctorResult:
        available, detail = await self._probe_source()
        return DoctorResult(
            name=self.name,
            available=available,
            ws_url=None,  # doctor never opens a ws — spec §5.2 contract
            detail=detail,
            ux_cost=self.ux_cost,
        )

    async def resolve(self, timeout: float) -> ResolveResult:
        ws_url, extras = await self._resolve_source(timeout)
        return ResolveResult(ws_url=ws_url, backend=self.name, extras=extras)

    # ---- URL-source hooks ---------------------------------------------------

    async def _probe_source(self) -> tuple[bool, str]:
        raise NotImplementedError

    async def _resolve_source(self, timeout: float) -> tuple[str, dict]:
        raise NotImplementedError

    # ---- shared plumbing ----------------------------------------------------

    async def _get(self, url: str, timeout: float) -> httpx.Response:
        async with httpx.AsyncClient(timeout=timeout, trust_env=self._trust_env) as client:
            return await client.get(url)

    async def _discover_ws_url(self, base_url: str, timeout: float) -> str:
        """HTTP GET {base_url}/json/version and return webSocketDebuggerUrl.

        Spec §8.1/§8.2: this is the standard CDP HTTP discovery shape that all
        real and fingerprint browsers, plus most cloud providers, agree on.
        The ONE copy shared by every real-CDP URL source, so the paths can
        never diverge on edge handling (timeout / non-200 / missing key).
        """
        url = f"{base_url.rstrip('/')}/json/version"
        try:
            resp = await self._get(url, timeout)
        except (httpx.HTTPError, OSError) as e:
            raise Unavailable(
                f"{self.name}: cannot reach {url}: {e}",
                attempts={self.name: f"GET {url} -> {type(e).__name__}: {e}"},
            ) from e
        if resp.status_code == 404:
            raise _JsonVersion404(
                f"{self.name}: {url} returned HTTP 404",
                attempts={self.name: f"GET {url} -> HTTP 404"},
            )
        if resp.status_code != 200:
            raise Unavailable(
                f"{self.name}: {url} returned HTTP {resp.status_code}",
                attempts={self.name: f"GET {url} -> HTTP {resp.status_code}"},
            )
        try:
            body = resp.json()
        except ValueError as e:
            raise Unavailable(
                f"{self.name}: {url} returned non-JSON: {e}",
                attempts={self.name: f"GET {url} -> non-JSON"},
            ) from e
        ws = body.get("webSocketDebuggerUrl") if isinstance(body, dict) else None
        if not isinstance(ws, str) or not ws:
            raise Unavailable(
                f"{self.name}: {url} JSON has no webSocketDebuggerUrl",
                attempts={self.name: f"GET {url} -> missing webSocketDebuggerUrl"},
            )
        return ws


# ---- rdp: local Chrome on --remote-debugging-port ---------------------------


class RdpBackend(RealCdpBackend):
    name = "rdp"
    _trust_env = False  # localhost-only; see RealCdpBackend._trust_env

    def __init__(self, cfg: Config):
        super().__init__(cfg)
        self.port = cfg.backends.rdp.port

    async def _probe_source(self) -> tuple[bool, str]:
        """Does HTTP discovery succeed, OR (404-case) does some profile's
        DevToolsActivePort point at this port?"""
        port = self.port
        url = f"http://127.0.0.1:{port}/json/version"
        try:
            resp = await self._get(url, 1.0)
        except (httpx.HTTPError, OSError) as e:
            return False, f"no service on 127.0.0.1:{port} ({type(e).__name__})"
        if resp.status_code == 200:
            # 200 == happy path: discovery works. We don't actually parse
            # webSocketDebuggerUrl here — `resolve` does that for real.
            return True, f"HTTP 200 from {url}"
        if resp.status_code == 404:
            # Chrome 136/147+ default-profile lockdown. Probe by matching the
            # port number in the existing DevToolsActivePort files. No ws open.
            matched = _find_matching_profile(port)
            if matched is not None:
                return True, (
                    f"HTTP 404 from {url} (Chrome 136/147+ default-profile "
                    f"lockdown); fallback matched {matched}/DevToolsActivePort"
                )
            return False, (
                f"HTTP 404 from {url}; no DevToolsActivePort file on port {port} "
                "in known profiles. Chrome likely needs --user-data-dir=<isolated>."
            )
        return False, f"HTTP {resp.status_code} from {url}"

    async def _resolve_source(self, timeout: float) -> tuple[str, dict]:
        url = f"http://127.0.0.1:{self.port}/json/version"
        try:
            ws = await self._discover_ws_url(f"http://127.0.0.1:{self.port}", timeout)
        except _JsonVersion404:
            ws = _ws_from_devtools_active_port(url)
            if ws is None:
                raise Unavailable(
                    f"rdp: HTTP 404 on {url} (Chrome 136/147+ default-profile lockdown) "
                    f"and no matching DevToolsActivePort file in known profiles",
                    attempts={self.name: f"GET {url} -> 404, fallback no match"},
                ) from None
            matched_profile = _find_matching_profile(self.port)
            return ws, {
                "isolated_profile": False,  # default-profile lockdown ⇒ user is on default
                "profile_path": str(matched_profile) if matched_profile else None,
            }
        return ws, {"isolated_profile": None, "profile_path": None}


# ---- env: caller-injected URL (BD_CDP_WS / BD_CDP_URL) -----------------------


class EnvBackend(RealCdpBackend):
    name = "env"

    async def _probe_source(self) -> tuple[bool, str]:
        cfg = self._cfg
        if cfg.cdp_ws:
            return True, _origin_detail(
                "BD_CDP_WS" if cfg.cdp_ws_source == "BD_CDP_WS" else "BU_CDP_WS")
        if cfg.cdp_url:
            return True, _origin_detail(
                "BD_CDP_URL" if cfg.cdp_url_source == "BD_CDP_URL" else "BU_CDP_URL")
        return False, "BD_CDP_WS not set, BD_CDP_URL not set"

    async def _resolve_source(self, timeout: float) -> tuple[str, dict]:
        cfg = self._cfg
        if cfg.cdp_ws:
            return cfg.cdp_ws, {}  # trusted verbatim — see module docstring
        if cfg.cdp_url:
            ws = await self._discover_ws_url(cfg.cdp_url, timeout)
            return ws, {"discovery_url": cfg.cdp_url}
        raise Unavailable(
            "env backend: neither BD_CDP_WS nor BD_CDP_URL is set",
            attempts={self.name: "BD_CDP_WS not set, BD_CDP_URL not set"},
        )


def _origin_detail(source: str) -> str:
    if source.startswith("BU_"):
        return f"{source} set (deprecated, please rename to BD_{source[3:]})"
    return f"{source} set"


# ---- DevToolsActivePort helpers (rdp 404-fallback) ---------------------------


def _find_matching_profile(want_port: int) -> Path | None:
    """Walk PROFILES, return the first whose DevToolsActivePort line 1 == want_port
    AND whose line 2 is a non-empty ws path.

    Multiple matches → mtime-newest.
    """
    matches: list[tuple[float, Path]] = []
    for base in profile_paths():
        f = base / "DevToolsActivePort"
        try:
            lines = f.read_text().splitlines()
            mtime = f.stat().st_mtime
        except (FileNotFoundError, NotADirectoryError, PermissionError, OSError):
            continue
        if not lines:
            continue
        port = lines[0].strip()
        ws_path = lines[1].strip() if len(lines) > 1 else ""
        if port == str(want_port) and ws_path:
            matches.append((mtime, base))
    if not matches:
        return None
    return max(matches, key=lambda t: t[0])[1]


def _ws_from_devtools_active_port(http_url: str) -> str | None:
    """Build a ws:// URL from a DevToolsActivePort file when /json/version 404s.

    Ported from browser-harness daemon.py:83-101 — preserves the IPv6 bracket
    handling (urlparse strips brackets; we restore them) and the line-1/line-2
    contract.
    """
    p = urlparse(http_url)
    want_port = str(p.port) if p.port else ""
    if not want_port:
        return None
    host = p.hostname or "127.0.0.1"
    if ":" in host:  # IPv6 literal
        host = f"[{host}]"
    for base in profile_paths():
        try:
            active = (base / "DevToolsActivePort").read_text().splitlines()
        except (FileNotFoundError, NotADirectoryError, PermissionError, OSError):
            continue
        port = active[0].strip() if active else ""
        ws_path = active[1].strip() if len(active) > 1 else ""
        if port == want_port and ws_path:
            return f"ws://{host}:{port}{ws_path}"
    return None
