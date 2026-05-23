"""extension backend — v0.4 real implementation (spec §8.4).

Architecture: this backend doesn't return an upstream ws URL — Chrome's
CDP isn't the upstream here. Instead, the daemon (Mode B `serve`) starts a
local relay ws server (`server/relay.py`) on `127.0.0.1:19989`, and the
user's Chrome extension connects TO us. The daemon then proxies standard
CDP between Skill clients and the extension's `chrome.debugger` API.

Implications:

- `kind=LOCAL_RELAY` (spec §4.3): not a URL passthrough; the daemon
  emulates the upstream side.
- `resolve()` in Mode A still raises — Mode A `url` is meaningless when the
  daemon IS the relay; Skill must drive Mode B.
- `probe()` does a real check: GET `http://127.0.0.1:19989/__status__` and
  inspect the response for a connected extension. If reachable + at least
  one extension has sent `hello`, `available=true`.
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

import httpx

from ..config import Config
from ..errors import Unavailable
from .base import Backend, DoctorResult, ResolveResult
from ..server.relay import DEFAULT_RELAY_PORT

logger = logging.getLogger(__name__)


class ExtensionBackend(Backend):
    name = "extension"
    kind = "LOCAL_RELAY"
    recommended_mode: str = "B"
    ux_cost = "extension-permission"

    def __init__(self, cfg: Config):
        self._cfg = cfg
        # v0.5.3 F-5 / Task #24: single source of truth for host+port.
        # Honors CLI --extension-port > BD_EXTENSION_PORT > toml port >
        # toml relay_url > DEFAULT_RELAY_PORT.
        self._host, self._port = cfg.backends.extension.resolved_host_port()

    def caps(self) -> dict:
        # Attaches to the user's existing Chrome; isolates sessions via tab
        # groups, not browser contexts (P4).
        return {"owns_browser": False, "supports_browser_context": False}

    async def probe(self) -> DoctorResult:
        """HTTP GET 127.0.0.1:19989/__status__.

        Three outcomes:
          1. connection refused → no daemon-serve running with extension
             backend → available=false + needs_user_action
          2. running but `extensions == 0` → relay up, user hasn't installed
             / loaded the extension → available=false + needs_user_action
          3. running with extensions → available=true
        """
        url = f"http://127.0.0.1:{self._port}/__status__"
        # `trust_env=False` + explicit `mounts={}` makes httpx ignore the
        # user's HTTPS_PROXY / SOCKS_PROXY / ALL_PROXY env vars. Loopback
        # traffic must never go through a proxy — same trick the other
        # backends use for their `/json/version` probes.
        try:
            async with httpx.AsyncClient(
                timeout=2.0, trust_env=False, mounts={},
            ) as client:
                resp = await client.get(url)
        except (httpx.ConnectError, httpx.ConnectTimeout):
            return DoctorResult(
                name=self.name,
                available=False,
                ws_url=None,
                detail=(f"no extension relay listening on 127.0.0.1:{self._port} "
                        f"— start `browserwright-daemon serve --backend extension`"),
                ux_warning=None,
                needs_user_action=(
                    "start daemon in Mode B with extension backend, "
                    "then load the Chrome extension from `chrome-extension/`"),
                ux_cost=self.ux_cost,
            )
        except Exception as e:
            return DoctorResult(
                name=self.name,
                available=False,
                ws_url=None,
                detail=f"relay status probe failed: {e!r}",
                ux_warning=None,
                needs_user_action="check daemon logs",
                ux_cost=self.ux_cost,
            )

        if resp.status_code != 200:
            return DoctorResult(
                name=self.name,
                available=False,
                ws_url=None,
                detail=f"relay /__status__ returned HTTP {resp.status_code}",
                needs_user_action="restart daemon-serve",
                ux_cost=self.ux_cost,
            )
        try:
            status = resp.json()
        except (ValueError, json.JSONDecodeError):
            status = {}

        ext_count = int(status.get("extensions", 0))
        if ext_count == 0:
            return DoctorResult(
                name=self.name,
                available=False,
                ws_url=None,
                detail=("extension relay is running but no Chrome extension "
                        "has connected yet"),
                needs_user_action=(
                    "load `chrome-extension/` as an unpacked extension in "
                    "Chrome (chrome://extensions/ → enable Developer mode "
                    "→ Load unpacked)"),
                ux_cost=self.ux_cost,
            )

        install_ids = status.get("install_ids") or []
        tabs = int(status.get("tab_count", 0))
        return DoctorResult(
            name=self.name,
            available=True,
            ws_url=None,
            detail=(f"{ext_count} extension(s) connected "
                    f"(install_ids={install_ids}, attached tabs={tabs})"),
            ux_warning=None,
            needs_user_action=None,
            ux_cost=self.ux_cost,
        )

    async def resolve(self, timeout: float) -> ResolveResult:
        """Mode A `url` isn't meaningful for LOCAL_RELAY: there's no
        externally-connectable upstream ws — the daemon IS the relay. So we
        raise Unavailable with a hint that points to Mode B.
        """
        raise Unavailable(
            "extension backend is a LOCAL_RELAY — it cannot be used via "
            "`browserwright-daemon url`. Run `browserwright-daemon serve --backend "
            "extension` instead and connect via the daemon's unix socket.",
            attempts={self.name: "LOCAL_RELAY backend requires Mode B serve"},
        )
