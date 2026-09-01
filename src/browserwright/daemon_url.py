"""The one address every downstream uses to reach the daemon (ADR-0011).

Since the unix control socket was deleted, *everything* — the CLI, the skill
client, the executor data plane, a Playwright ``connect_over_cdp`` — reaches the
daemon through a single TCP endpoint. This module is the only place that decides
what that endpoint's URL is.

Precedence, highest first::

    --daemon-url <url>      (CLI flag, recorded via `set_cli_daemon_url`)
    BW_DAEMON_URL=<url>     (env)
    daemon_url = "<url>"    (the toml: --config, else $BD_CONFIG)
    <the daemon's own bound-endpoint state file>
    http://127.0.0.1:19990  (default)

The first three sources are **explicit**: the operator named an endpoint, so a
failed connection is an error and the client must never auto-start or restart a
daemon — a remote daemon is not ours to spawn, and even a hand-written localhost
URL says "I am pointing you at a daemon I manage". The state file and the
default are *not* explicit; those keep today's auto-start / version-skew restart
behavior.

The state file is written by the daemon at bind time and removed at shutdown. It
exists for one reason: a daemon told to bind port 0 (the test-isolation scheme)
knows its port only after binding. It is deliberately ranked BELOW every
configured source and above only the default, so a stale file can at worst cost
one failed ping — after which the default auto-start path takes over.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from urllib.parse import quote, urlsplit

#: Where a browserwright client looks when nothing is configured. Matches
#: ``DEFAULT_FACADE_PORT`` / ``DEFAULT_FACADE_HOST`` in ``daemon/config.py``,
#: which remain the daemon-side *bind* knobs for the same endpoint.
DEFAULT_DAEMON_URL = "http://127.0.0.1:19990"

#: Env var carrying the endpoint URL. The only ``BW_``-prefixed variable in the
#: product; every pre-existing daemon variable keeps its ``BD_`` prefix.
ENV_VAR = "BW_DAEMON_URL"

_cli_override: str | None = None
_cli_config_path: str | None = None


def set_cli_daemon_url(url: str | None) -> None:
    """Record a ``--daemon-url`` flag. Highest precedence, process-wide.

    Called from both CLIs during argument parsing, before anything resolves an
    endpoint. Passing ``None`` clears it (used by tests)."""
    global _cli_override
    _cli_override = url.strip() if isinstance(url, str) and url.strip() else None


def cli_daemon_url() -> str | None:
    return _cli_override


def set_cli_config_path(path: str | None) -> None:
    """Record a ``--config`` flag so the toml source can be read from it.

    `Config.load()` resolves its toml as ``--config`` **then** ``$BD_CONFIG``,
    and the ``daemon_url`` key has to obey the same order or
    ``browserwright-daemon --config custom.toml`` would configure the daemon
    from one file while addressing it from another — silently operating on the
    wrong daemon. Recorded process-wide, like the URL flag, because endpoint
    resolution happens far from argument parsing."""
    global _cli_config_path
    _cli_config_path = path.strip() if isinstance(path, str) and path.strip() else None


def cli_config_path() -> str | None:
    return _cli_config_path


@dataclass(frozen=True)
class DaemonEndpoint:
    """A resolved endpoint address plus where it came from.

    ``explicit`` is the load-bearing half: it is what every auto-start and
    auto-restart site gates on (ADR-0011).
    """

    url: str
    explicit: bool
    source: str

    @property
    def host(self) -> str:
        return urlsplit(self.url).hostname or "127.0.0.1"

    @property
    def port(self) -> int:
        return urlsplit(self.url).port or 19990

    @property
    def authority(self) -> str:
        return f"{self.host}:{self.port}"

    @property
    def is_loopback(self) -> bool:
        """Whether this endpoint's host is a loopback address."""
        import ipaddress
        host = self.host
        if host in ("localhost", "localhost."):
            return True
        try:
            return ipaddress.ip_address(host).is_loopback
        except ValueError:
            return False

    @property
    def is_locally_signalable(self) -> bool:
        """Whether a pid this endpoint reports can be signalled on this machine.

        `stop` and `restart` work by pinging for a pid and signalling it, which
        is only sound while the daemon runs here. Before ADR-0011 that was
        guaranteed — the endpoint was a unix socket in our own runtime dir. A
        URL is not, so this is the replacement guarantee, and it keys on the
        endpoint's **source**, not its host:

        - **state file** — published by a daemon *on this machine* when it
          bound (the file sits in our runtime dir and carries that daemon's
          pid). Its host is whatever `--facade-host` said, which is routinely
          non-loopback: exposing the endpoint over Tailscale is the documented
          remote-access setup, and a bind to a specific IP does not listen on
          127.0.0.1 at all. Signalling is safe and must keep working — this is
          the daemon's *own* machine.
        - **default** — loopback by construction.
        - **explicit** (`--daemon-url` / `$BW_DAEMON_URL` / toml) — someone
          named an address. Loopback still means here; anything else may name
          another host, where the reported pid belongs to a stranger's process
          or, worse, happens to match an unrelated local one. Refuse.
        """
        return self.is_loopback or not self.explicit

    @property
    def http(self) -> str:
        """The ``http://host:port`` form, no trailing slash."""
        return f"http://{self.authority}"

    def ws(self, path: str, **query: str | None) -> str:
        """Build ``ws://host:port<path>?<query>`` for one of the three surfaces.

        ``None``-valued query params are dropped, so callers can pass an
        optional session id straight through.
        """
        qs = "&".join(
            f"{k}={quote(str(v), safe='')}"
            for k, v in query.items() if v is not None and v != ""
        )
        return f"ws://{self.authority}{path}" + (f"?{qs}" if qs else "")


def _from_toml(env: dict) -> str | None:
    # Same precedence as `Config.load()`: the `--config` flag, then $BD_CONFIG.
    path = cli_config_path() or env.get("BD_CONFIG")
    if not path:
        return None
    import tomllib
    from pathlib import Path
    try:
        data = tomllib.loads(Path(path).expanduser().read_text())
    except (OSError, ValueError):
        return None
    value = data.get("daemon_url") if isinstance(data, dict) else None
    return value.strip() if isinstance(value, str) and value.strip() else None


def _from_state_file() -> str | None:
    """The URL the running daemon published when it bound its endpoint."""
    import json
    from .daemon import _ipc
    try:
        data = json.loads(_ipc.endpoint_state_path().read_text())
    except (OSError, ValueError):
        return None
    url = data.get("url") if isinstance(data, dict) else None
    return url if isinstance(url, str) and url else None


def daemon_endpoint(*, env: dict | None = None) -> DaemonEndpoint:
    """Resolve the daemon endpoint. See the module docstring for precedence."""
    e = os.environ if env is None else env
    cli = cli_daemon_url()
    if cli:
        return DaemonEndpoint(url=_normalize(cli), explicit=True, source="cli")
    from_env = (e.get(ENV_VAR) or "").strip()
    if from_env:
        return DaemonEndpoint(url=_normalize(from_env), explicit=True,
                              source="env")
    from_toml = _from_toml(e)
    if from_toml:
        return DaemonEndpoint(url=_normalize(from_toml), explicit=True,
                              source="toml")
    from_state = _from_state_file()
    if from_state:
        return DaemonEndpoint(url=_normalize(from_state), explicit=False,
                              source="state_file")
    return DaemonEndpoint(url=DEFAULT_DAEMON_URL, explicit=False,
                          source="default")


def daemon_url(*, env: dict | None = None) -> tuple[str, bool]:
    """``(url, explicit)`` — the shorthand most call sites want."""
    ep = daemon_endpoint(env=env)
    return ep.url, ep.explicit


def _normalize(url: str) -> str:
    """Accept ``host:port`` / ``ws://…`` / ``http://…`` and return http form."""
    text = url.strip().rstrip("/")
    if "://" not in text:
        text = f"http://{text}"
    parts = urlsplit(text)
    scheme = "http" if parts.scheme in ("ws", "http") else (
        "https" if parts.scheme in ("wss", "https") else "http")
    host = parts.hostname or "127.0.0.1"
    port = parts.port or 19990
    if ":" in host:  # IPv6 literal
        host = f"[{host}]"
    return f"{scheme}://{host}:{port}"


#: The message every "explicitly configured, but nothing answered" path shows.
def unreachable_message(ep: DaemonEndpoint) -> str:
    return (
        f"no browserwright daemon answered at {ep.url} "
        f"(from {_SOURCE_LABEL.get(ep.source, ep.source)}). Because the "
        "endpoint was configured explicitly, browserwright will not start or "
        "restart a daemon for you: start it on that machine with "
        "`browserwright-daemon serve` (bind it with `--facade-host` so it is "
        "reachable), or unset the setting to use the local default "
        f"{DEFAULT_DAEMON_URL}."
    )


_SOURCE_LABEL = {
    "cli": "--daemon-url",
    "env": f"${ENV_VAR}",
    "toml": "the `daemon_url` config key",
}


def local_unreachable_fix(ep: DaemonEndpoint) -> str:
    """The `fix` for "nothing answered" on a NON-explicitly-configured endpoint.

    The class default — "start the single global daemon: `browserwright-daemon
    serve`" — is a dead end whenever the daemon is already running, which is
    the common case here: the daemon bound a *specific* non-loopback host
    (`--facade-host <tailnet-ip>`) and this client resolved something else. So
    name the real divergence instead of guessing.
    """
    published = _from_state_file()
    normalized = _normalize(published) if published else None

    if ep.source == "default" and normalized and normalized != ep.url:
        return (
            f"a daemon published {normalized} in its endpoint state file, but "
            f"this client resolved the built-in default {ep.url} — they "
            f"disagree. Point the client at it (`export {ENV_VAR}="
            f"{normalized}`), or rebind the daemon so loopback is served too "
            "(`browserwright-daemon install --facade-host 0.0.0.0` then "
            "`browserwright-daemon restart`)."
        )
    if ep.source == "state_file":
        host_note = ""
        if not ep.is_loopback:
            host_note = (
                f" That endpoint is bound to the non-loopback host "
                f"{ep.host}, so it is only reachable over that interface — if "
                "it is down (VPN/tailnet off), nothing local can reach the "
                "daemon."
            )
        return (
            f"the running daemon published {ep.url} but nothing answered "
            f"there.{host_note} Check it with `browserwright-daemon status` "
            "and `lsof -nP -iTCP:"
            f"{ep.port} -sTCP:LISTEN`, then `browserwright-daemon restart`. "
            "If the daemon is up, the state file is stale."
        )
    return (
        f"nothing is listening on the default endpoint {ep.url}. Check "
        f"`browserwright-daemon status`; if a daemon IS running it is bound "
        "elsewhere (see `--facade-host`) — point this client at it with "
        f"${ENV_VAR}. Otherwise start one: `browserwright-daemon serve`. "
        f"Note `lsof -nP -iTCP:{ep.port} -sTCP:LISTEN` shows a foreign holder "
        "of the port (a proxy such as Surge can answer HTTP on it without a "
        "daemon behind it)."
    )


def not_ours_to_signal_message(ep: DaemonEndpoint, action: str) -> str:
    """Why a local signal-based ``action`` is refused for a remote endpoint."""
    return (
        f"the daemon endpoint is {ep.url} (from "
        f"{_SOURCE_LABEL.get(ep.source, ep.source)}), which is not this "
        "machine. "
        f"`{action}` works by signalling a process id, and the id that endpoint "
        "reports belongs to its own machine — signalling it here could hit an "
        f"unrelated local process. Run `{action}` on the machine that serves "
        f"{ep.url}, or unset the endpoint setting to manage the local daemon."
    )


def child_env(env: dict | None = None) -> dict:
    """The environment to hand a child ``browserwright-daemon`` process.

    A `--daemon-url` flag lives in *this* process's memory and does not
    propagate the way `$BW_DAEMON_URL` does, so a child CLI would resolve the
    default endpoint instead — pass the explicit-endpoint liveness gate against
    one daemon and then mutate a different one. Materializing the resolved URL
    into the child's environment closes that gap.

    Only an **explicit** endpoint is exported: an inherited default or state
    file already resolves identically in the child, and exporting it would
    additionally flip the child into the "never auto-start" regime, which is
    exactly wrong for the `serve` we may be spawning.
    """
    import os as _os
    out = dict(_os.environ if env is None else env)
    ep = daemon_endpoint(env=out)
    if ep.explicit:
        out[ENV_VAR] = ep.url
    return out
