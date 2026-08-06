"""Real-CDP backend — ONE implementation, two URL sources.

Real browser-level CDP over a ws URL, `kind=UPSTREAM_WS`, treated by the Router
as a raw-CDP upstream. `RealCdpBackend` carries everything shared — the class
attributes, the DoctorResult/ResolveResult plumbing, and the single
`/json/version` discovery (`_discover_ws_url`). The two URL sources are:

- **a local port** (spec §8.2): Chrome launched with
  `--remote-debugging-port=NNNN`, discovered via `/json/version` on
  `127.0.0.1:port`. The interesting part is the Chrome 136/147+
  default-profile lockdown: those builds disable HTTP discovery when the
  user-data-dir is the *real* user profile (privacy hardening), returning a
  404. The websocket path still works, and Chrome still writes it into
  `DevToolsActivePort`. So the fallback is: HTTP 404 → walk PROFILES, match
  the port number on line 1, read the ws path from line 2. This mirrors
  browser-harness `daemon.py:83-101` `_ws_from_devtools_active_port` — which
  has already eaten the IPv6-host-bracket and stale-port edges. That file is
  on *this* machine's disk, so the fallback is local-only by construction.

- **an injected endpoint** (spec §8.1 + §8.1.1): a URL supplied per session via
  `--attach=<url>`. `ws(s)://` is trusted verbatim — no parsing, no rewriting,
  which is the whole point for cloud / fingerprint browsers with URL-embedded
  tokens. `http(s)://` goes through the same `/json/version` discovery pointed
  at an arbitrary host.

Until #38 these were two backends, `rdp` and `env`, and the endpoint came from
the process-global `BD_CDP_WS` / `BD_CDP_URL` — which is why one daemon could
only ever reach one external browser. The endpoint is now per-session ledger
state, and the two backends collapsed into this one; see `RdpBackend` for what
replaced the two name checks they differed by.
"""
from __future__ import annotations

from pathlib import Path
from urllib.parse import urlparse

import httpx

from .._net import is_loopback_host, redact_url
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
        # `base_url` may be a per-session endpoint carrying a token in its
        # userinfo or query. Every string below reaches the client AND the
        # daemon log, so the URL is redacted for reporting while the real one
        # is what we actually GET.
        shown = redact_url(url)
        try:
            resp = await self._get(url, timeout)
        except (httpx.HTTPError, OSError) as e:
            raise Unavailable(
                f"{self.name}: cannot reach {shown}: {e}",
                attempts={self.name: f"GET {shown} -> {type(e).__name__}: {e}"},
            ) from e
        if resp.status_code == 404:
            raise _JsonVersion404(
                f"{self.name}: {shown} returned HTTP 404",
                attempts={self.name: f"GET {shown} -> HTTP 404"},
            )
        if resp.status_code != 200:
            raise Unavailable(
                f"{self.name}: {shown} returned HTTP {resp.status_code}",
                attempts={self.name: f"GET {shown} -> HTTP {resp.status_code}"},
            )
        try:
            body = resp.json()
        except ValueError as e:
            raise Unavailable(
                f"{self.name}: {shown} returned non-JSON: {e}",
                attempts={self.name: f"GET {shown} -> non-JSON"},
            ) from e
        ws = body.get("webSocketDebuggerUrl") if isinstance(body, dict) else None
        if not isinstance(ws, str) or not ws:
            raise Unavailable(
                f"{self.name}: {shown} JSON has no webSocketDebuggerUrl",
                attempts={self.name: f"GET {shown} -> missing webSocketDebuggerUrl"},
            )
        return ws


# ---- the one real-CDP backend -----------------------------------------------


class RdpBackend(RealCdpBackend):
    """Real browser-level CDP. Two URL sources, one class.

    - **no endpoint** — a browser on this machine at `backends.rdp.port`,
      discovered via `http://127.0.0.1:<port>/json/version`. Either one we
      launched (`--create`) or a local one we were pointed at (`--attach=9222`).
    - **endpoint set** — a URL handed to us for this session (`--attach=<url>`).
      `ws(s)://` is the endpoint itself; `http(s)://` is a discovery URL.

    Both used to be separate backends (`rdp` and `env`) whose only real
    difference was two `if self.name == ...`-shaped decisions. Both decisions
    are the same physical question — *is this browser on my machine?* — so the
    merge replaces two name checks with one predicate, `_loopback`:

    | | old | new |
    |---|---|---|
    | apply the user's `ALL_PROXY` | `rdp` no, `env` yes | `not _loopback` |
    | DevToolsActivePort 404 fallback | `rdp` yes, `env` never | `_loopback` |

    That is strictly more correct than the names were: `--attach=http://127.0.0.1:9222`
    now gets the proxy bypass and the Chrome-136 lockdown fallback, which the
    old `env` path denied it for no reason other than what it was called.
    """
    name = "rdp"

    def __init__(self, cfg: Config):
        super().__init__(cfg)
        self.port = cfg.backends.rdp.port
        self.endpoint = cfg.backends.rdp.endpoint

    # ---- the one predicate the two old backends disagreed about -------------

    @property
    def _loopback(self) -> bool:
        """Is the target browser on this machine?"""
        if self.endpoint is None:
            return True  # port mode is always 127.0.0.1
        return is_loopback_host(self.endpoint)

    @property
    def _trust_env(self) -> bool:  # overrides RealCdpBackend's class attribute
        """Apply the user's HTTP(S)_PROXY / ALL_PROXY?

        Never for a local browser: proxying to your own loopback is never what
        anyone wants, and it trips httpx[socks] import errors under
        `ALL_PROXY=socks5://...`. Always for a remote endpoint, where reaching
        it through the proxy is usually the whole point.
        """
        return not self._loopback

    # ---- URL sources --------------------------------------------------------

    async def _probe_source(self) -> tuple[bool, str]:
        if self.endpoint is not None:
            # Cheap by contract (spec §5.2): report that an endpoint is
            # configured without dialling it.
            return True, f"endpoint configured: {redact_url(self.endpoint)}"
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
        ep = self.endpoint
        if ep is None:
            return await self._resolve_http(f"http://127.0.0.1:{self.port}", timeout)
        scheme = urlparse(ep).scheme.lower()
        if scheme in ("ws", "wss"):
            # Verbatim — no parsing, no rewriting. This is the whole point of
            # accepting a URL: cloud and anti-detect browsers embed tokens in
            # it, and any "helpful" normalisation would invalidate them.
            return ep, {"isolated_profile": None, "profile_path": None}
        if scheme in ("http", "https"):
            ws, extras = await self._resolve_http(ep, timeout)
            return ws, {**extras, "discovery_url": redact_url(ep)}
        raise Unavailable(
            f"{self.name}: unsupported endpoint scheme {scheme!r} — expected "
            f"ws, wss, http or https",
            attempts={self.name: f"endpoint scheme {scheme!r}"},
        )

    async def _resolve_http(self, base_url: str, timeout: float) -> tuple[str, dict]:
        """`/json/version` discovery, with the local-only 404 fallback."""
        url = f"{base_url.rstrip('/')}/json/version"
        try:
            ws = await self._discover_ws_url(base_url, timeout)
        except _JsonVersion404:
            # The DevToolsActivePort file is on THIS machine's disk, so it can
            # only speak for a local browser. A remote 404 is just a 404.
            port = urlparse(base_url).port
            if not self._loopback or port is None:
                raise
            ws = _ws_from_devtools_active_port(url)
            if ws is None:
                raise Unavailable(
                    f"{self.name}: HTTP 404 on {url} (Chrome 136/147+ default-profile "
                    f"lockdown) and no matching DevToolsActivePort file in known profiles",
                    attempts={self.name: f"GET {url} -> 404, fallback no match"},
                ) from None
            matched_profile = _find_matching_profile(port)
            return ws, {
                "isolated_profile": False,  # default-profile lockdown ⇒ user is on default
                "profile_path": str(matched_profile) if matched_profile else None,
            }
        return ws, {"isolated_profile": None, "profile_path": None}


# ---- DevToolsActivePort helpers (local 404-fallback) -------------------------


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
