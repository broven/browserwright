"""Config + env merge.

Source of truth ordering (highest precedence first):
  CLI flag  >  BD_*-prefixed env var  >  BU_*-prefixed env var (compat)  >  toml file  >  defaults

Spec §5.1 environment-variable table lives here. The toml shape is intentionally
flat — pydantic / dynaconf are explicitly rejected (附录 B).

The toml file is *optional*. If it doesn't parse, we report a UserError up-stream
so the CLI can show a clean message instead of a stack trace.
"""
from __future__ import annotations

import os
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path


# Path-traversal guard for BD_NAME — sourced from browser-harness _ipc.py:31-33.
# Matches the regex spec §6.7 nails down.
_NAME_RE = re.compile(r"\A[A-Za-z0-9_-]{1,64}\Z")


def check_name(name: str) -> str:
    if not _NAME_RE.match(name or ""):
        from .errors import UserError

        raise UserError(
            f"invalid BD_NAME {name!r}: must match [A-Za-z0-9_-]{{1,64}}"
        )
    return name


@dataclass
class RdpConfig:
    port: int = 9222


@dataclass
class ExtensionConfig:
    """v0.5.3 REVIEW.md F-5 / Task #24: relay endpoint config.

    Two ways to override the default `ws://127.0.0.1:19989`:
      - `relay_url`: full URL — most expressive, can change host too
      - `port`: int — common case ("19989 is occupied, use 29989")

    Precedence (highest first):
      CLI `--extension-port N`  >  `BD_EXTENSION_PORT` env  >  toml `port`
        >  toml `relay_url` (parsed for port)  >  `DEFAULT_RELAY_PORT` (19989)

    `host` follows the same source: explicit `port` knobs preserve the
    existing host (or 127.0.0.1 default), `relay_url` carries both.
    """
    relay_url: str | None = None
    port: int | None = None

    def resolved_host_port(self) -> tuple[str, int]:
        """Single source of truth for "what host:port should the relay
        ws server bind to" — consumers (ExtensionBackend ctor for probing,
        listener.run_serve for binding) call this so they don't each
        re-implement the precedence rules.

        Returns `(host, port)`. `port` is non-None — we fall back to
        `DEFAULT_RELAY_PORT` from server/relay.py if nothing else is set.
        """
        # Local import dodges the circular hazard between config and server.
        from .server.relay import DEFAULT_RELAY_PORT
        host = "127.0.0.1"
        port = DEFAULT_RELAY_PORT
        if self.relay_url:
            from urllib.parse import urlparse
            parsed = urlparse(self.relay_url)
            if parsed.hostname:
                host = parsed.hostname
            if parsed.port:
                port = parsed.port
        if self.port is not None:
            port = self.port
        return host, port


@dataclass
class CloudConfig:
    """v0.5 cloud backend config.

    `endpoint` is the upstream URL. Two shapes accepted:
      - `wss://host/path` → used directly as the ws URL
      - `https://host`    → daemon HTTP-GETs `/json/version` and reads
                             `webSocketDebuggerUrl` (same trick as the `env`
                             backend's `BD_CDP_URL` path)

    `auth_kind` picks one of the registered AuthProvider impls in
    `browserwright.daemon.auth`. The kind-specific config dict is `auth` (raw
    toml subtable, dispatched by `build_auth_provider`).

    `provider_hint` is purely informational — surfaces in doctor output
    ("connected to provider=browser-use, auth=bearer"). The daemon doesn't
    behave differently per provider.
    """

    endpoint: str | None = None
    auth_kind: str | None = None  # "bearer" | "basic" | "mtls" | "oauth2"
    auth: dict = field(default_factory=dict)  # raw, fed to build_auth_provider
    provider_hint: str | None = None  # display name, e.g. "browser-use"


@dataclass
class BackendsConfig:
    rdp: RdpConfig = field(default_factory=RdpConfig)
    cloud: CloudConfig = field(default_factory=CloudConfig)
    extension: ExtensionConfig = field(default_factory=ExtensionConfig)


@dataclass
class Config:
    """Resolved daemon config — flat, immutable after build."""

    name: str = "default"
    backend: str | None = None             # explicit --backend / BD_BACKEND
    timeout: float = 5.0                   # per-backend resolve timeout, seconds
    cdp_ws: str | None = None              # BD_CDP_WS / BU_CDP_WS — env backend uses this
    cdp_url: str | None = None             # BD_CDP_URL / BU_CDP_URL — env backend uses this
    chrome_binary: str | None = None       # BD_CHROME_BINARY — launch-chrome
    idle_close_after: float | None = None  # seconds; None = never (default)
    backends: BackendsConfig = field(default_factory=BackendsConfig)
    # Provenance for `env`: which alias key actually fired. Diagnostic-only.
    cdp_ws_source: str | None = None       # "BD_CDP_WS" | "BU_CDP_WS" | None
    cdp_url_source: str | None = None      # "BD_CDP_URL" | "BU_CDP_URL" | None


def _read_toml(path: Path) -> dict:
    from .errors import UserError

    try:
        return tomllib.loads(path.read_text())
    except FileNotFoundError:
        return {}
    except (tomllib.TOMLDecodeError, OSError) as e:
        raise UserError(f"failed to parse config {path}: {e}") from e


def load(
    cli_backend: str | None = None,
    cli_timeout: float | None = None,
    cli_port: int | None = None,
    cli_chrome_binary: str | None = None,
    cli_config_path: str | None = None,
    cli_name: str | None = None,
    cli_extension_port: int | None = None,
    env: dict[str, str] | None = None,
) -> Config:
    """Build a Config from CLI flags + env + optional toml file.

    `env` defaults to os.environ — injecting a dict lets unit tests pin state.
    """
    e = os.environ if env is None else env

    # 1) Optional toml
    cfg_path = cli_config_path or e.get("BD_CONFIG")
    toml: dict = {}
    if cfg_path:
        toml = _read_toml(Path(cfg_path).expanduser())

    # 2) Build a Config piece by piece, with each level overriding the prior
    cfg = Config()

    # toml level
    if "name" in toml and isinstance(toml["name"], str):
        cfg.name = toml["name"]
    if "timeout" in toml and isinstance(toml["timeout"], (int, float)):
        cfg.timeout = float(toml["timeout"])
    # v0.5.2 Task #14: `default_backend` from config.toml. The README has
    # advertised this key since v0.1 but the parser silently ignored it —
    # users wrote it expecting it to lock the backend and got the auto
    # fallback chain instead. Precedence (highest first):
    #   CLI --backend  >  BD_BACKEND env  >  toml `default_backend`
    # Backend-name validity is checked at resolve-time (we don't import
    # backends.names at module scope — circular), so an invalid name like
    # `"garbage"` is stored as-is and surfaces at `browserwright-daemon url` as
    # UserError("unknown backend ...").
    # v0.5.3 F-9 #14: type is validated though — `default_backend = 42`
    # (integer) silently dropped before, now raises so the user can fix it.
    if "default_backend" in toml:
        v = toml["default_backend"]
        if not isinstance(v, str):
            from .errors import UserError
            raise UserError(
                f"config.toml `default_backend` must be a string, got "
                f"{type(v).__name__}: {v!r}")
        cfg.backend = v
    backends = toml.get("backends", {}) if isinstance(toml.get("backends"), dict) else {}
    rdp = backends.get("rdp", {}) if isinstance(backends.get("rdp"), dict) else {}
    if "port" in rdp and isinstance(rdp["port"], int):
        cfg.backends.rdp.port = rdp["port"]
    # extension.relay_url substitutes for DEFAULT_RELAY_PORT when set.
    ext = backends.get("extension", {}) if isinstance(backends.get("extension"), dict) else {}
    if isinstance(ext.get("relay_url"), str):
        cfg.backends.extension.relay_url = ext["relay_url"]
    if isinstance(ext.get("port"), int):
        cfg.backends.extension.port = ext["port"]
    cloud = backends.get("cloud", {}) if isinstance(backends.get("cloud"), dict) else {}
    if "endpoint" in cloud and isinstance(cloud["endpoint"], str):
        cfg.backends.cloud.endpoint = cloud["endpoint"]
    if "auth_kind" in cloud and isinstance(cloud["auth_kind"], str):
        cfg.backends.cloud.auth_kind = cloud["auth_kind"]
    if "provider_hint" in cloud and isinstance(cloud["provider_hint"], str):
        cfg.backends.cloud.provider_hint = cloud["provider_hint"]
    # `[backends.cloud.auth.<kind>]` subtables are passed verbatim to
    # `build_auth_provider`. We pick out the subtable matching `auth_kind`
    # so the config-time schema stays per-kind validated downstream.
    auth_subtables = cloud.get("auth", {}) if isinstance(cloud.get("auth"), dict) else {}
    if cfg.backends.cloud.auth_kind and isinstance(
            auth_subtables.get(cfg.backends.cloud.auth_kind), dict):
        cfg.backends.cloud.auth = dict(auth_subtables[cfg.backends.cloud.auth_kind])
    if "idle_close_after" in toml and isinstance(toml["idle_close_after"], (int, float)):
        cfg.idle_close_after = float(toml["idle_close_after"])

    # env level — BD_* wins over BU_*
    if "BD_NAME" in e:
        cfg.name = e["BD_NAME"]
    if "BD_TIMEOUT" in e:
        try:
            cfg.timeout = float(e["BD_TIMEOUT"])
        except ValueError:
            from .errors import UserError

            raise UserError(f"BD_TIMEOUT must be a number, got {e['BD_TIMEOUT']!r}")
    if "BD_BACKEND" in e:
        cfg.backend = e["BD_BACKEND"]
    if "BD_IDLE_CLOSE_AFTER" in e:
        try:
            v = float(e["BD_IDLE_CLOSE_AFTER"])
            cfg.idle_close_after = v if v > 0 else None
        except ValueError:
            from .errors import UserError
            raise UserError(
                f"BD_IDLE_CLOSE_AFTER must be a number, got {e['BD_IDLE_CLOSE_AFTER']!r}")
    if "BD_CDP_WS" in e:
        cfg.cdp_ws = e["BD_CDP_WS"]; cfg.cdp_ws_source = "BD_CDP_WS"
    elif "BU_CDP_WS" in e:
        cfg.cdp_ws = e["BU_CDP_WS"]; cfg.cdp_ws_source = "BU_CDP_WS"
    if "BD_CDP_URL" in e:
        cfg.cdp_url = e["BD_CDP_URL"]; cfg.cdp_url_source = "BD_CDP_URL"
    elif "BU_CDP_URL" in e:
        cfg.cdp_url = e["BU_CDP_URL"]; cfg.cdp_url_source = "BU_CDP_URL"
    if "BD_CHROME_BINARY" in e:
        cfg.chrome_binary = e["BD_CHROME_BINARY"]
    # `BD_RDP_PORT` env override for the rdp backend's port (v0.4.1).
    #
    # Originally (spec §8.2 first cut) we deliberately omitted this env var:
    # the rdp port was config-file or `--port` only, to keep the env namespace
    # small. But the ai-e2e harness convention is "set env, run agent" — and
    # without `BD_RDP_PORT`, callers reach for `BD_BACKEND=rdp` + (nothing),
    # which silently falls through to the hard-coded 9222 default, which on
    # any developer machine **is the user's daily Chrome** (Chrome 144+ auto-
    # enables CDP without a cmdline flag). Connecting to that = Allow popup.
    #
    # Adding the env var makes "lock to my isolated Chrome's port" expressible
    # without a config file. Precedence: CLI `--port` > BD_RDP_PORT > toml >
    # 9222 default (preserves the spec §5.1 ordering rule).
    if "BD_RDP_PORT" in e:
        try:
            cfg.backends.rdp.port = int(e["BD_RDP_PORT"])
        except ValueError:
            from .errors import UserError
            raise UserError(
                f"BD_RDP_PORT must be an integer, got {e['BD_RDP_PORT']!r}")
    elif "BD_PORT" in e:
        # v0.5.3 REVIEW.md F-4c: the v0.4 popup-storm incident's root cause
        # was a typo: `BD_PORT=9444` (intuitive name) didn't bind to anything
        # and the rdp backend defaulted to 9222 = user's daily Chrome. We
        # added `BD_RDP_PORT` but never defended the typo. Now we alias +
        # warn: if user typed BD_PORT and not BD_RDP_PORT, accept the value
        # and print a deprecation hint to stderr so they migrate.
        try:
            cfg.backends.rdp.port = int(e["BD_PORT"])
        except ValueError:
            from .errors import UserError
            raise UserError(
                f"BD_PORT (alias for BD_RDP_PORT) must be an integer, got "
                f"{e['BD_PORT']!r}")
        # Stderr (NOT stdout — `browserwright-daemon url`'s stdout is the URL).
        # In tests we sometimes silently want to set the env without warning
        # noise; gate on `BD_PORT_QUIET=1` for that.
        if e.get("BD_PORT_QUIET", "") not in ("1", "true", "True"):
            import sys
            print(
                f"warning: BD_PORT is a deprecated alias for BD_RDP_PORT — "
                f"please update your env / script. (BD_PORT={e['BD_PORT']!r} "
                f"applied to rdp.port.)",
                file=sys.stderr,
            )
    # v0.5 cloud backend env overrides — useful for one-off CLI calls
    # without writing a config.toml. Each maps to the equivalent toml key.
    if "BD_CLOUD_ENDPOINT" in e:
        cfg.backends.cloud.endpoint = e["BD_CLOUD_ENDPOINT"]
    if "BD_CLOUD_AUTH_KIND" in e:
        cfg.backends.cloud.auth_kind = e["BD_CLOUD_AUTH_KIND"]
    if "BD_CLOUD_PROVIDER_HINT" in e:
        cfg.backends.cloud.provider_hint = e["BD_CLOUD_PROVIDER_HINT"]
    # v0.5.3 Task #24: extension relay port via env. Symmetric to BD_RDP_PORT
    # — useful when the default 19989 is occupied by a stale daemon process
    # and the user can't write a config.toml on the fly. (playwriter sits on
    # 19988, so default conflict with it is no longer a concern.)
    if "BD_EXTENSION_PORT" in e:
        try:
            cfg.backends.extension.port = int(e["BD_EXTENSION_PORT"])
        except ValueError:
            from .errors import UserError
            raise UserError(
                f"BD_EXTENSION_PORT must be an integer, got "
                f"{e['BD_EXTENSION_PORT']!r}")
    # The auth payload itself is read from kind-specific env vars that the
    # AuthProvider already knows about (`token_env`, `cert_file`, etc).
    # The env-override layer here is just for endpoint/kind selection; we
    # deliberately don't have `BD_CLOUD_TOKEN` style shortcuts because
    # that would mean either (a) silently overwriting `auth.bearer.token`
    # without provenance tracking, or (b) inventing a parallel resolution
    # path the AuthProvider can't see. Better to use `BROWSER_USE_API_KEY`
    # + `token_env="BROWSER_USE_API_KEY"` in config.toml.

    # CLI level — last word
    if cli_backend is not None:
        cfg.backend = cli_backend
    if cli_timeout is not None:
        cfg.timeout = cli_timeout
    if cli_port is not None:
        cfg.backends.rdp.port = cli_port
    if cli_chrome_binary is not None:
        cfg.chrome_binary = cli_chrome_binary
    if cli_name is not None:
        cfg.name = cli_name
    if cli_extension_port is not None:
        # v0.5.3 Task #24: CLI tops env / toml for the extension relay port.
        cfg.backends.extension.port = cli_extension_port

    # Validate name as the very last step (in case env injected garbage)
    check_name(cfg.name)
    return cfg
