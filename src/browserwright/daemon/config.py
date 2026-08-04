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


# Path-traversal guard for the `launch-chrome --profile` name — sourced from
# browser-harness _ipc.py:31-33. (No longer used for a daemon instance name;
# BD_NAME was removed — see docs/refactor-single-daemon.md.)
_NAME_RE = re.compile(r"\A[A-Za-z0-9_-]{1,64}\Z")

# Default port for the Playwright-facing CDP facade. Distinct from the
# extension relay (19989) and playwriter's 19988 so all three can coexist on one
# machine. Defined here (not facade.py) so config has no import cycle; facade.py
# re-exports it for backwards compat.
DEFAULT_FACADE_PORT = 19990

# Default bind host for the Playwright-facing CDP facade. Stays loopback so the
# facade is NEVER reachable off-box by accident; override via `--facade-host` /
# `BD_FACADE_HOST` / `facade_host` in config.toml to bind a Tailscale/LAN
# interface (e.g. `0.0.0.0` or a specific tailnet IP) for remote CDP clients.
DEFAULT_FACADE_HOST = "127.0.0.1"


def check_name(name: str) -> str:
    """Path-traversal guard for filesystem-bound names (e.g. `--profile`)."""
    if not _NAME_RE.match(name or ""):
        from .errors import UserError

        raise UserError(
            f"invalid name {name!r}: must match [A-Za-z0-9_-]{{1,64}}"
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
class BackendsConfig:
    rdp: RdpConfig = field(default_factory=RdpConfig)
    extension: ExtensionConfig = field(default_factory=ExtensionConfig)


@dataclass
class Config:
    """Resolved daemon config — flat, immutable after build."""

    backend: str | None = None             # explicit --backend / BD_BACKEND
    timeout: float = 5.0                   # per-backend resolve timeout, seconds
    cdp_ws: str | None = None              # BD_CDP_WS / BU_CDP_WS — env backend uses this
    cdp_url: str | None = None             # BD_CDP_URL / BU_CDP_URL — env backend uses this
    chrome_binary: str | None = None       # BD_CHROME_BINARY — launch-chrome
    idle_close_after: float | None = None  # seconds; None = never (default)
    session_idle_prune: float | None = 24 * 3600  # ledger prune threshold
    # Playwright-facing CDP facade. `serve` binds an additional TCP ws+HTTP
    # endpoint that a real Playwright client can `connect_over_cdp` to.
    #
    # Phase C semantics (auto-enable): the facade is now ON by default — the
    # skill layer's heredoc `page`/`context` depend on it. Tri-state:
    #   - None (unset)  -> auto-enable on DEFAULT_FACADE_PORT (the new default).
    #   - 0             -> explicitly DISABLED.
    #   - >0            -> explicit override port (CLI/env/toml).
    # Use `resolved_facade_port()` to collapse this to "bind / don't bind".
    facade_port: int | None = None
    # Bind host for the Playwright facade. Defaults to loopback (never exposed by
    # accident); set to a Tailscale/LAN IP or `0.0.0.0` to reach it off-box.
    # Precedence mirrors facade_port: CLI `--facade-host` > `BD_FACADE_HOST` env
    # > toml `facade_host` > DEFAULT_FACADE_HOST.
    facade_host: str = DEFAULT_FACADE_HOST
    backends: BackendsConfig = field(default_factory=BackendsConfig)
    # Provenance for `env`: which alias key actually fired. Diagnostic-only.
    cdp_ws_source: str | None = None       # "BD_CDP_WS" | "BU_CDP_WS" | None
    cdp_url_source: str | None = None      # "BD_CDP_URL" | "BU_CDP_URL" | None

    def resolved_facade_port(self) -> int | None:
        """Collapse the tri-state ``facade_port`` to a bind decision.

        Returns the port to bind the Playwright facade on, or ``None`` when the
        facade should NOT be bound:
          - ``facade_port is None``  -> ``DEFAULT_FACADE_PORT`` (auto-enable).
          - ``facade_port == 0``     -> ``None`` (explicitly disabled).
          - ``facade_port > 0``      -> that port (explicit override).
        """
        if self.facade_port is None:
            return DEFAULT_FACADE_PORT
        if self.facade_port == 0:
            return None
        return self.facade_port


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
    cli_extension_port: int | None = None,
    cli_facade_port: int | None = None,
    cli_facade_host: str | None = None,
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
    if "idle_close_after" in toml and isinstance(toml["idle_close_after"], (int, float)):
        cfg.idle_close_after = float(toml["idle_close_after"])
    if "session_idle_prune" in toml and isinstance(toml["session_idle_prune"], (int, float)):
        v = float(toml["session_idle_prune"])
        cfg.session_idle_prune = v if v > 0 else None
    # Playwright facade port (phase A1). toml key `facade_port`.
    if "facade_port" in toml and isinstance(toml["facade_port"], int):
        cfg.facade_port = toml["facade_port"]
    # Playwright facade bind host. toml key `facade_host`.
    if "facade_host" in toml and isinstance(toml["facade_host"], str):
        cfg.facade_host = toml["facade_host"]

    # env level — BD_* wins over BU_*
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
    if "BD_SESSION_IDLE_PRUNE" in e:
        try:
            v = float(e["BD_SESSION_IDLE_PRUNE"])
            cfg.session_idle_prune = v if v > 0 else None
        except ValueError:
            from .errors import UserError
            raise UserError(
                f"BD_SESSION_IDLE_PRUNE must be a number, got "
                f"{e['BD_SESSION_IDLE_PRUNE']!r}")
    if "BD_CDP_WS" in e:
        cfg.cdp_ws = e["BD_CDP_WS"]
        cfg.cdp_ws_source = "BD_CDP_WS"
    elif "BU_CDP_WS" in e:
        cfg.cdp_ws = e["BU_CDP_WS"]
        cfg.cdp_ws_source = "BU_CDP_WS"
    if "BD_CDP_URL" in e:
        cfg.cdp_url = e["BD_CDP_URL"]
        cfg.cdp_url_source = "BD_CDP_URL"
    elif "BU_CDP_URL" in e:
        cfg.cdp_url = e["BU_CDP_URL"]
        cfg.cdp_url_source = "BU_CDP_URL"
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
    # Playwright facade port via env (phase A1). Symmetric to BD_EXTENSION_PORT
    # — set BD_FACADE_PORT=19990 (or any free port) to enable the facade.
    if "BD_FACADE_PORT" in e:
        try:
            cfg.facade_port = int(e["BD_FACADE_PORT"])
        except ValueError:
            from .errors import UserError
            raise UserError(
                f"BD_FACADE_PORT must be an integer, got {e['BD_FACADE_PORT']!r}")
    # Playwright facade bind host via env. Symmetric to BD_FACADE_PORT — set
    # BD_FACADE_HOST=<tailnet-ip>/0.0.0.0 to reach the facade off-box.
    if "BD_FACADE_HOST" in e:
        cfg.facade_host = e["BD_FACADE_HOST"]

    # CLI level — last word
    if cli_backend is not None:
        cfg.backend = cli_backend
    if cli_timeout is not None:
        cfg.timeout = cli_timeout
    if cli_port is not None:
        cfg.backends.rdp.port = cli_port
    if cli_chrome_binary is not None:
        cfg.chrome_binary = cli_chrome_binary
    if cli_extension_port is not None:
        # v0.5.3 Task #24: CLI tops env / toml for the extension relay port.
        cfg.backends.extension.port = cli_extension_port
    if cli_facade_port is not None:
        # Phase A1: CLI `--facade-port` tops env / toml.
        cfg.facade_port = cli_facade_port
    if cli_facade_host is not None:
        # CLI `--facade-host` tops env / toml.
        cfg.facade_host = cli_facade_host

    return cfg
