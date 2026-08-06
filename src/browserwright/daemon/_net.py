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
- **`redact_url`** — a CDP endpoint routinely carries a reusable token, in the
  userinfo or the query string. Anything that prints an endpoint (daemon logs,
  `daemon ps --json`, `session list --json`, error messages) must go through
  this first.
"""
from __future__ import annotations

import ipaddress
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
    if host in _LOOPBACK_NAMES:
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


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
