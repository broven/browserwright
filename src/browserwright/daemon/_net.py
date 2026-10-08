"""Network primitives shared across the daemon.

A leaf module by construction — it imports nothing from `browserwright`, so
`daemon.server.*`, `daemon.backends.*`, and Layer 2 can all use it without a
cycle. Same role as `_ipc.py` / `_rpc.py` / `_stale.py`.

Two rules live here because each answers a question that is asked from more
than one place, and answering it twice is how the two copies drift apart:

- **`is_loopback_host`** — "is this browser on *my* machine?" That single
  question decides both whether the user's `ALL_PROXY` should apply and
  whether Chrome's `DevToolsActivePort` file is worth reading. Neither is
  correct for a remote endpoint.
- **`proxy_for`** — "which proxy reaches this attach target?" Answered once,
  by the CLI from *its* environment when a session is opened, and stored on
  the session (#136). The daemon never reads proxy env vars of its own.
- **`redact_url`** — a CDP endpoint routinely carries a reusable token, in the
  userinfo or the query string. Anything that prints an endpoint (daemon logs,
  `daemon ps --json`, `session list --json`, error messages) must go through
  this first.
"""
from __future__ import annotations

import ipaddress
from collections.abc import Mapping
from urllib.parse import urlsplit, urlunsplit

#: Hostnames that mean "this machine" without being parseable as an address.
#: The trailing-dot form is a legal FQDN spelling of the same name.
_LOOPBACK_NAMES = frozenset({"localhost", "localhost."})


def is_loopback_host(host_or_url: str) -> bool:
    """True when `host_or_url` names this machine.

    Accepts either a bare host (`127.0.0.1`, `::1`, `[::1]`, `localhost`) or a
    full URL to take the host from (`ws://127.0.0.1:9222/devtools/browser/x`).

    Uses `ipaddress` rather than a literal allowlist, so the whole `127.0.0.0/8`
    range answers True — a hand-written tuple of `("127.0.0.1", "localhost",
    "::1")` silently misses `127.0.0.2`, which Chrome will happily bind to.

    Anything unparseable is False: a host we cannot identify is treated as
    remote, which is the safe direction for both callers (proxy stays applied,
    local-only fallbacks stay off).
    """
    if not isinstance(host_or_url, str) or not host_or_url:
        return False
    host = host_or_url
    if "://" in host:
        try:
            host = urlsplit(host).hostname or ""
        except ValueError:
            return False
    # `urlsplit().hostname` already strips IPv6 brackets and lowercases; a bare
    # host string handed in directly may still carry either.
    host = host.strip().strip("[]").lower()
    if not host:
        return False
    # RFC 6761: every `*.localhost` name resolves to loopback.
    if host in _LOOPBACK_NAMES or host.rstrip(".").endswith(".localhost"):
        return True
    try:
        addr = ipaddress.ip_address(host)
    except ValueError:
        return False
    # `0.0.0.0` / `::` as a *destination* is this machine on every OS we run on.
    return addr.is_loopback or addr.is_unspecified


#: Which env var serves which target scheme. `ws` rides the same proxy as
#: `http` and `wss` the same as `https`, matching curl and `websockets`.
_PROXY_VAR_FOR_SCHEME = {"http": "http", "ws": "http",
                         "https": "https", "wss": "https"}


def _env_value(env: Mapping[str, str], name: str) -> str:
    """`name_proxy` from `env`, lowercase winning over uppercase as in curl."""
    for key in (name.lower(), name.upper()):
        value = env.get(key)
        if value and value.strip():
            return value.strip()
    return ""


def _no_proxy_matches(host: str, port: int | None, no_proxy: str) -> bool:
    """Does a `NO_PROXY` list exempt `host`?

    Entries are comma- or space-separated: `*` (everything), an IP or CIDR
    (`100.64.0.0/10`), or a domain, which also covers its subdomains whether
    written `example.com` or `.example.com`. An entry may carry a `:port`,
    which then has to match too. `urllib`'s own bypass check does not
    understand CIDR, which is the form a tailnet or LAN is usually written in.
    """
    try:
        addr = ipaddress.ip_address(host)
    except ValueError:
        addr = None
    for raw in no_proxy.replace(" ", ",").split(","):
        entry = raw.strip().lower()
        if not entry:
            continue
        if entry == "*":
            return True
        entry_port: int | None = None
        if entry.startswith("[") and "]" in entry:
            body, _, rest = entry[1:].partition("]")
            if rest.startswith(":") and rest[1:].isdigit():
                entry_port = int(rest[1:])
            entry = body
        elif entry.count(":") == 1:
            body, _, maybe_port = entry.partition(":")
            if maybe_port.isdigit():
                entry, entry_port = body, int(maybe_port)
        if entry_port is not None and entry_port != port:
            continue
        if addr is not None:
            try:
                if addr in ipaddress.ip_network(entry, strict=False):
                    return True
            except ValueError:
                pass
            continue
        domain = entry.lstrip(".").rstrip(".")
        if domain and (host == domain or host.endswith("." + domain)):
            return True
    return False


def proxy_for(url: str, env: Mapping[str, str]) -> str | None:
    """The proxy `env` says reaches `url`, or None for a direct connection.

    Reads exactly the `http_proxy` / `https_proxy` / `all_proxy` / `no_proxy`
    variables (either case) in `env` — never the OS proxy settings, which
    `urllib.request.getproxies()` falls back to on macOS. Loopback targets are
    always direct: proxying to your own machine is never what anyone wants.

    The caller passes `env` explicitly because *whose* environment it is is
    the whole decision (#136): the CLI that opens a session passes its own,
    and the daemon never calls this with its own.
    """
    if is_loopback_host(url):
        return None
    try:
        parts = urlsplit(url)
        host = (parts.hostname or "").lower()
        port = parts.port
    except ValueError:
        return None
    var = _PROXY_VAR_FOR_SCHEME.get(parts.scheme.lower())
    if not host or var is None:
        return None
    if _no_proxy_matches(host, port, _env_value(env, "no_proxy")):
        return None
    proxy = _env_value(env, f"{var}_proxy") or _env_value(env, "all_proxy")
    if not proxy:
        return None
    # `host:port` without a scheme is a common way to write an HTTP proxy.
    return proxy if "://" in proxy else f"http://{proxy}"


def proxy_toward(url: str, proxy: str | None) -> str | None:
    """`proxy`, unless `url` is on this machine.

    A session's proxy was resolved for its attach URL, but discovery can hand
    back a websocket URL on a different host. A loopback one is still direct.
    """
    return None if proxy is None or is_loopback_host(url) else proxy


def proxy_hint(proxy: str | None, url: str) -> str:
    """The suffix that turns a connect failure through `proxy` into an
    actionable one, or "" when the connection was direct.

    Without it a dead proxy reads as a dead remote browser.
    """
    if proxy is None:
        return ""
    try:
        host = urlsplit(url).hostname or url
    except ValueError:
        host = url
    return (f" (via proxy {redact_url(proxy)}, taken from the environment "
            f"that opened this session; if that proxy is not reachable, end "
            f"the session and open it again with NO_PROXY={host} set)")


def redact_url(url: object) -> object:
    """Strip credentials from a URL before it is reported anywhere.

    A CDP endpoint for a cloud or anti-detect browser routinely carries a
    reusable token — in the userinfo, or as a query parameter. Keep enough to
    identify the endpoint (scheme, host, port, path) and drop the rest: these
    fields exist to tell you *which* endpoint is in play, never to authenticate
    to it.

    Non-string input and strings that are not URLs are returned unchanged, so
    this is safe to apply blindly to a field that may hold anything.
    """
    if not isinstance(url, str) or "://" not in url:
        return url
    # `urlsplit` itself is lazy and does not validate — `.port` is what raises
    # on a non-numeric port, so the whole field-access block must be inside the
    # guard. (It wasn't, before this moved out of `status.py`: `daemon ps
    # --json` raised on such a URL and the `<unparseable>` branch was dead.)
    try:
        parts = urlsplit(url)
        netloc = parts.hostname or ""
        if parts.port is not None:
            netloc = f"{netloc}:{parts.port}"
        if parts.username or parts.password:
            netloc = f"<redacted>@{netloc}"
        query = "<redacted>" if parts.query else ""
        return urlunsplit((parts.scheme, netloc, parts.path, query, ""))
    except ValueError:
        # Never fall through returning the raw string: a malformed endpoint can
        # still carry a live token.
        return "<unparseable>"
