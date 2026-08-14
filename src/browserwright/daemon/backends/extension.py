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

import json
import logging

import httpx

from ..config import Config
from ..errors import Unavailable
from .base import Backend, DoctorResult, ResolveResult
from browserwright.version import EXTENSION_PROTOCOL_VERSION, package_version

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

    async def probe(self) -> DoctorResult:
        """HTTP GET 127.0.0.1:19989/__status__.

        Three outcomes:
          1. connection refused → no daemon-serve running with extension
             backend → available=false + needs_user_action
          2. running but `extensions == 0` → relay up, user hasn't installed
             / loaded the extension → available=false + needs_user_action
          3. running with extensions → available=true
        """
        from ..relay_status import fetch as fetch_relay_status
        try:
            resp = await fetch_relay_status("127.0.0.1", self._port)
        except (httpx.ConnectError, httpx.ConnectTimeout):
            return DoctorResult(
                name=self.name,
                available=False,
                ws_url=None,
                detail=(f"no extension relay listening on 127.0.0.1:{self._port} "
                        f"— start `browserwright-daemon serve`"),
                ux_warning=None,
                needs_user_action=(
                    "start the single global daemon, "
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
                    "install the browserwright extension from the Chrome Web "
                    "Store (https://chromewebstore.google.com/detail/"
                    "browserwright-daemon-rela/okgnalaalckoaeledbjhpjiccmcdceeb); "
                    "developers: chrome://extensions/ → enable Developer mode "
                    "→ Load unpacked → pick `chrome-extension/`"),
                ux_cost=self.ux_cost,
            )

        install_ids = status.get("install_ids") or []
        details = status.get("extension_details") or []
        incompatible = [
            item for item in details
            if item.get("compatible") is False
        ]
        version_mismatch = [
            item for item in details
            if item.get("browserwright_version")
            and item.get("browserwright_version") != package_version()
        ]
        if len(details) >= 2:
            # Two connected extensions (e.g. the store build + an unpacked dev
            # load in the same profile) make the daemon's choice of which
            # extension answers a tab ambiguous. Warn instead of guessing.
            return DoctorResult(
                name=self.name,
                available=True,
                ws_url=None,
                detail=(
                    f"{len(details)} extension(s) connected "
                    f"(install_ids={install_ids})"
                ),
                ux_warning=(
                    "multiple browserwright extensions are connected "
                    "(store + unpacked?); keep exactly one installed or the "
                    "daemon may drive the wrong one"
                ),
                needs_user_action=None,
                ux_cost=self.ux_cost,
            )
        if incompatible:
            return DoctorResult(
                name=self.name,
                available=False,
                ws_url=None,
                detail=(
                    "extension relay connected, but protocol is incompatible "
                    f"(daemon expects {EXTENSION_PROTOCOL_VERSION})"
                ),
                ux_warning=(
                    "reload the unpacked extension from the matching release; "
                    "a store install auto-updates once a compatible version is "
                    "published there"
                ),
                needs_user_action=(  # noqa: SIM114 - wording differs by install source
                    "update the extension: a store install updates itself; "
                    "an unpacked load needs `browserwright-daemon extension "
                    "reload` (or re-upload to the Chrome Web Store)"
                ),
                ux_cost=self.ux_cost,
            )
        warning = None
        if version_mismatch:
            seen = sorted({
                str(item.get("browserwright_version"))
                for item in version_mismatch
                if item.get("browserwright_version")
            })
            store = any(
                item.get("install_source") == "store"
                for item in details
            )
            warning = (
                f"extension version(s) {seen} do not match package "
                f"{package_version()}"
                + ("; the store build auto-updates once a matching version "
                   "is published" if store else "; reload the unpacked extension")
            )
        tabs = int(status.get("tab_count", 0))
        return DoctorResult(
            name=self.name,
            available=True,
            ws_url=None,
            detail=(f"{ext_count} extension(s) connected "
                    f"(install_ids={install_ids}, attached tabs={tabs})"),
            ux_warning=warning,
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
            "`browserwright-daemon url`. Run `browserwright-daemon serve` "
            "instead and connect via the daemon's unix socket.",
            attempts={self.name: "LOCAL_RELAY backend requires Mode B serve"},
        )
