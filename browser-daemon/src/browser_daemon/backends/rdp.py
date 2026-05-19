"""rdp backend — Chrome launched with --remote-debugging-port=NNNN.

Spec §8.2. The interesting part is the Chrome 136/147+ default-profile lockdown:
those builds disable `/json/version` HTTP discovery when the user-data-dir is
the *real* user profile (privacy hardening), returning a 404. The websocket
path still works, and Chrome still writes it into `DevToolsActivePort`. So the
fallback is: HTTP 404 → walk PROFILES, match the port number on line 1, read
the ws path from line 2.

This mirrors browser-harness `daemon.py:83-101` `_ws_from_devtools_active_port`
— which has already eaten the IPv6-host-bracket and stale-port edges.
"""
from __future__ import annotations

from pathlib import Path
from urllib.parse import urlparse

import httpx

from ..config import Config
from ..errors import Unavailable
from ..platforms import profile_paths
from .base import Backend, DoctorResult, ResolveResult


class RdpBackend(Backend):
    name = "rdp"
    kind = "UPSTREAM_WS"
    recommended_mode: str = "A"
    ux_cost = "none"

    def __init__(self, cfg: Config):
        self._cfg = cfg
        self.port = cfg.backends.rdp.port

    # ------------------------------------------------------------------ probe

    async def probe(self) -> DoctorResult:
        """Cheap reachability check — does HTTP discovery succeed, OR (404-case)
        does some profile's DevToolsActivePort point at this port?

        Per spec §5.2, probe never opens a ws and never produces ws_url.
        """
        port = self.port
        url = f"http://127.0.0.1:{port}/json/version"
        try:
            # trust_env=False keeps the user's HTTP(S)_PROXY / ALL_PROXY from
            # being applied to localhost probes — proxying to your own loopback
            # is never what anyone wants here and triggers httpx[socks] import
            # errors when ALL_PROXY=socks5://...
            async with httpx.AsyncClient(timeout=1.0, trust_env=False) as client:
                resp = await client.get(url)
        except (httpx.HTTPError, OSError) as e:
            return DoctorResult(
                name=self.name,
                available=False,
                ws_url=None,
                detail=f"no service on 127.0.0.1:{port} ({type(e).__name__})",
                ux_cost=self.ux_cost,
            )
        if resp.status_code == 200:
            # 200 == happy path: discovery works. We don't actually parse
            # webSocketDebuggerUrl here — `resolve` does that for real.
            return DoctorResult(
                name=self.name,
                available=True,
                ws_url=None,
                detail=f"HTTP 200 from {url}",
                ux_cost=self.ux_cost,
            )
        if resp.status_code == 404:
            # Chrome 136/147+ default-profile lockdown. Probe by matching the
            # port number in the existing DevToolsActivePort files. No ws open.
            matched = _find_matching_profile(port)
            if matched is not None:
                return DoctorResult(
                    name=self.name,
                    available=True,
                    ws_url=None,
                    detail=(
                        f"HTTP 404 from {url} (Chrome 136/147+ default-profile "
                        f"lockdown); fallback matched {matched}/DevToolsActivePort"
                    ),
                    ux_cost=self.ux_cost,
                )
            return DoctorResult(
                name=self.name,
                available=False,
                ws_url=None,
                detail=(
                    f"HTTP 404 from {url}; no DevToolsActivePort file on port {port} "
                    "in known profiles. Chrome likely needs --user-data-dir=<isolated>."
                ),
                ux_cost=self.ux_cost,
            )
        return DoctorResult(
            name=self.name,
            available=False,
            ws_url=None,
            detail=f"HTTP {resp.status_code} from {url}",
            ux_cost=self.ux_cost,
        )

    # ---------------------------------------------------------------- resolve

    async def resolve(self, timeout: float) -> ResolveResult:
        port = self.port
        url = f"http://127.0.0.1:{port}/json/version"
        try:
            async with httpx.AsyncClient(timeout=timeout, trust_env=False) as client:
                resp = await client.get(url)
        except (httpx.HTTPError, OSError) as e:
            raise Unavailable(
                f"rdp: cannot reach {url}: {e}",
                attempts={self.name: f"GET {url} -> {type(e).__name__}: {e}"},
            ) from e
        if resp.status_code == 200:
            try:
                body = resp.json()
            except ValueError as e:
                raise Unavailable(
                    f"rdp: {url} returned non-JSON: {e}",
                    attempts={self.name: f"GET {url} -> non-JSON"},
                ) from e
            ws = body.get("webSocketDebuggerUrl") if isinstance(body, dict) else None
            if not isinstance(ws, str) or not ws:
                raise Unavailable(
                    f"rdp: {url} JSON has no webSocketDebuggerUrl",
                    attempts={self.name: "no webSocketDebuggerUrl field"},
                )
            return ResolveResult(
                ws_url=ws,
                backend=self.name,
                extras={"isolated_profile": None, "profile_path": None},
            )
        if resp.status_code == 404:
            ws = _ws_from_devtools_active_port(url)
            if ws is None:
                raise Unavailable(
                    f"rdp: HTTP 404 on {url} (Chrome 136/147+ default-profile lockdown) "
                    f"and no matching DevToolsActivePort file in known profiles",
                    attempts={self.name: f"GET {url} -> 404, fallback no match"},
                )
            matched_profile = _find_matching_profile(self.port)
            return ResolveResult(
                ws_url=ws,
                backend=self.name,
                extras={
                    "isolated_profile": False,  # default-profile lockdown ⇒ user is on default
                    "profile_path": str(matched_profile) if matched_profile else None,
                },
            )
        raise Unavailable(
            f"rdp: {url} returned HTTP {resp.status_code}",
            attempts={self.name: f"GET {url} -> HTTP {resp.status_code}"},
        )


# ---- helpers (module-private, also imported by tests) ----------------------

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
