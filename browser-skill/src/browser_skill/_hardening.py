"""Production-side hardening (REVIEW.md F-4b).

Previously the popup-defense logic lived only in ``ai-e2e-tests/harness.py``;
a regular user running ``browser-skill <<'PY' ... PY`` from a shell never
benefited. This module ports the two production-relevant checks:

  1. **Port-9222 listener detection** — refuse to proceed when a process
     is listening on Chrome's autoconnect default port unless the caller
     explicitly opted in. That listener is almost always the user's
     daily Chrome (Chrome 144+ auto-enables CDP); a daemon fallback that
     drifts onto it pops the Allow dialog and Chrome accumulates those
     popups until it freezes (memory: chrome-popup-accumulation-bug).
  2. **Daemon-url cross-check** — when ``browser-daemon url`` is
     reachable, verify it doesn't resolve to ``127.0.0.1:9222`` unless
     the caller pinned that explicitly. Catches the ``BD_PORT=<typo>``
     class of misconfigurations.

Both checks run at the same entry points the popup-cost gate runs at
(``repl/inline.py``, ``repl/server.py``, ``cli.py`` subcommand dispatch).
Opt-out via ``BS_PRODUCTION_HARDENING=0`` (default on); opt-out for the
port-listener check specifically via ``BS_ALLOW_PORT_9222_LISTENER=1``
(equivalent to the harness CLI flag).

Tests live in ``tests/test_production_hardening.py``.
"""
from __future__ import annotations

import os
import socket
import subprocess
import sys
from typing import Optional


AUTOCONNECT_DEFAULT_PORT = 9222
_PORT_PROBE_TIMEOUT = 0.5
_DAEMON_URL_TIMEOUT = 5.0


class ProductionHardeningRefused(RuntimeError):
    """Raised at CLI entry when a production-hardening assertion fails.
    Subclasses ``RuntimeError`` so existing ``except RuntimeError`` paths
    catch it, but the explicit type makes ``isinstance()`` checks in
    tests + logs easier."""


def _truthy(value: Optional[str]) -> bool:
    return (value or "").strip().lower() in {"1", "true", "yes", "on"}


def _hardening_enabled() -> bool:
    """Off only when ``BS_PRODUCTION_HARDENING`` is explicitly disabled."""
    raw = os.environ.get("BS_PRODUCTION_HARDENING")
    if raw is None:
        return True
    return _truthy(raw)


def _port_is_listening(host: str, port: int, *,
                       timeout: float = _PORT_PROBE_TIMEOUT) -> bool:
    """Cheap accept-only probe — open + immediately close. No data sent."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        s.connect((host, port))
    except OSError:
        return False
    finally:
        s.close()
    return True


def _ws_targets_default_port(ws_url: str) -> bool:
    """``True`` when ``ws_url`` is a CDP URL pointing at localhost:9222.
    Conservative match — we only refuse on the exact default port, not
    on every localhost ws (the user may run a legitimate isolated
    Chrome on a custom port and surface it via ``BS_CDP_WS``)."""
    if not ws_url:
        return False
    needle = f":{AUTOCONNECT_DEFAULT_PORT}/"
    return needle in ws_url


def assert_safe_environment(*, allow_port_9222_listener: bool = False,
                            on_warning=None) -> None:
    """Refuse to start when the user's daily Chrome appears to be on
    ``:9222`` and the caller hasn't pinned a non-default endpoint.

    Skipped entirely when ``BS_PRODUCTION_HARDENING`` is disabled.
    Skipped when the caller has provided ``BS_CDP_WS`` / ``BU_CDP_WS``
    pointing at a non-default port (the user knows what they're doing).
    Skipped when ``BS_ALLOW_PORT_9222_LISTENER=1`` or the explicit
    keyword argument is set.

    Raises ``ProductionHardeningRefused`` with a user-actionable message
    on refusal. ``on_warning`` is called with the warning string when
    the bypass flag is set (for tests / structured logging).
    """
    if not _hardening_enabled():
        return

    # If the user has pinned an explicit ws and it's NOT the default
    # port, they've already opted out of the autoconnect path.
    explicit_ws = (os.environ.get("BS_CDP_WS")
                   or os.environ.get("BU_CDP_WS") or "")
    if explicit_ws and not _ws_targets_default_port(explicit_ws):
        return

    # Likewise, an explicit BD_BACKEND that isn't autoconnect skips this
    # check — the user has chosen rdp / extension / cloud / env and
    # whatever they're doing is their responsibility.
    bd_backend = os.environ.get("BD_BACKEND") or ""
    if bd_backend and bd_backend != "autoconnect":
        return

    if not _port_is_listening("127.0.0.1", AUTOCONNECT_DEFAULT_PORT):
        return

    bypass = allow_port_9222_listener or _truthy(
        os.environ.get("BS_ALLOW_PORT_9222_LISTENER"))
    if bypass:
        msg = (
            f"[browser-skill] WARNING: a Chrome is listening on "
            f":{AUTOCONNECT_DEFAULT_PORT} (likely the user's daily Chrome). "
            f"Proceeding because BS_ALLOW_PORT_9222_LISTENER=1; the "
            f"popup-cost gate downstream remains active."
        )
        if on_warning is not None:
            on_warning(msg)
        else:
            print(msg, file=sys.stderr)
        return

    raise ProductionHardeningRefused(
        f"Refusing to start: a Chrome is listening on "
        f":{AUTOCONNECT_DEFAULT_PORT} (autoconnect default port). This is "
        f"almost certainly the user's daily Chrome — Chrome 144+ "
        f"accumulates 'Allow remote debugging?' popups until it freezes "
        f"(memory: chrome-popup-accumulation-bug). Options:\n"
        f"  * shut down the Chrome on :{AUTOCONNECT_DEFAULT_PORT}, OR\n"
        f"  * run `browser-daemon launch-chrome --port <isolated> --profile "
        f"/tmp/<unique>` and pick option 1 in the install wizard, OR\n"
        f"  * set BS_ALLOW_PORT_9222_LISTENER=1 if you know what you're "
        f"doing (the popup-cost gate still runs)."
    )


def assert_daemon_url_safe(*, daemon_bin: str = "browser-daemon",
                            on_warning=None) -> None:
    """Cross-check ``browser-daemon url`` doesn't resolve to ``:9222``.

    Catches the ``BD_PORT=<typo>`` misconfiguration where the rdp
    backend silently falls back to 9222 and Skill would unwittingly
    target the user's daily Chrome. Daemon-impl-2's F-4c added
    a deprecation warning for that typo path; this assertion is the
    Skill-side belt-and-braces.

    Skipped when production hardening is disabled, when ``browser-daemon``
    isn't on PATH (no daemon installed = nothing to assert), or when
    ``BS_ALLOW_PORT_9222_LISTENER=1`` (same bypass — explicit opt-in
    semantics).
    """
    if not _hardening_enabled():
        return
    if _truthy(os.environ.get("BS_ALLOW_PORT_9222_LISTENER")):
        return

    try:
        proc = subprocess.run(
            [daemon_bin, "url"],
            capture_output=True, text=True,
            timeout=_DAEMON_URL_TIMEOUT,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return  # no daemon → nothing to assert
    if proc.returncode != 0:
        return  # daemon errored — separate failure mode; doctor / inline
                # gate will surface it

    url = (proc.stdout or "").strip().splitlines()[0:1]
    url = url[0] if url else ""
    if not url:
        return
    if not _ws_targets_default_port(url):
        return

    raise ProductionHardeningRefused(
        f"Refusing to start: `{daemon_bin} url` resolved to {url!r}, which "
        f"contains the autoconnect default port :{AUTOCONNECT_DEFAULT_PORT}. "
        f"This is almost certainly the user's daily Chrome. The most common "
        f"cause is BD_PORT set to a typo (daemon-impl-2 added a deprecation "
        f"warning for BD_PROT etc. — check your env for typos). To proceed:\n"
        f"  * fix the env (BD_PORT, BD_BACKEND), OR\n"
        f"  * set BS_ALLOW_PORT_9222_LISTENER=1 if you know what you're "
        f"doing.\n"
        f"Set BS_PRODUCTION_HARDENING=0 to disable this check globally."
    )


def assert_safe_or_warn(*, allow_port_9222_listener: bool = False) -> None:
    """Convenience: run both assertions, in the order F-4b spec'd. CLI
    entry points call this; tests drive the underlying functions
    directly."""
    assert_safe_environment(allow_port_9222_listener=allow_port_9222_listener)
    assert_daemon_url_safe()
