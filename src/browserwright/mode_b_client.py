"""Mode B daemon client — long-lived connection to the daemon endpoint.

Mode B is the happy path:

  - Skill connects to a running ``browserwright-daemon serve`` instance over the
    daemon's one TCP endpoint, on its **control surface** (ADR-0011).
  - Standard CDP commands are tunnelled through. ``BrowserwrightDaemon.*`` RPCs
    (``getActiveTab``, ``disconnect``, ``subscribeFocus``, ``uiState``) are
    answered by the daemon itself, not forwarded upstream.
  - Events fan out to the client: ``upstreamClosed``, ``activeTabChanged``,
    ``upstreamReady`` etc.

The Skill side here is a single-threaded sync wrapper that ``Session`` holds
as its sole daemon client (Mode A — the one-shot subprocess resolver — was
removed; the skill always talks to a running daemon over its socket).

Discovery:
  - The endpoint URL comes from :mod:`browserwright.daemon_url` — ``--daemon-url``
    / ``$BW_DAEMON_URL`` / the toml ``daemon_url`` key / the running daemon's
    state file / ``http://127.0.0.1:19990``. No subprocess, no socket path.
  - On connect, the client opens
    ``ws://<host>:<port>/control?client=skill-repl&session=<id>``.

**Explicitly configured endpoint ⇒ hands off.** When the URL came from a flag,
the env or the config file, this client never spawns a daemon and never
restarts one over a version skew: a daemon at an address someone chose — even
`127.0.0.1` — is not ours to manage, and on another machine we could not
restart it anyway. Only the unconfigured default keeps the old auto-start and
version-coherence behavior.

:func:`client_for_session` builds the client from a resolved ledger record;
``DaemonUnavailable`` surfaces lazily when no daemon answers.
"""
from __future__ import annotations

import json
import subprocess
from typing import Any, Optional

from .daemon_url import (
    DaemonEndpoint,
    child_env,
    daemon_endpoint,
    unreachable_message,
)
from .errors import DaemonUnavailable


class ModeBClient:
    """Mode B daemon endpoint. Use ``connect()`` to confirm reachability;
    ``ws_url()`` returns the CDP-compatible URL Skill's ``CDPSession`` can
    open. Active-tab / disconnect / uiState are sent over the same socket.
    """

    def __init__(self) -> None:
        self._endpoint: Optional[str] = None
        self._transport: Optional[str] = None  # always "tcp"
        self._cached_ws: Optional[str] = None
        # client label sent on the ws query string for daemon observability;
        # session-bound clients override this with ``skill-s<id>``.
        self._client_label: str = "skill-repl"
        # The session id, emitted on the ws query as ``?session=<id>`` — this is
        # the key the daemon's dispatcher routes on (cdp sessions reach their
        # own UpstreamContext through it). None for the bare REPL client.
        self._session_id: Optional[str] = None

    # ---- endpoint discovery ---------------------------------------------

    def endpoint(self) -> DaemonEndpoint:
        """The resolved daemon endpoint (URL + whether it was configured)."""
        return daemon_endpoint()

    @property
    def explicit(self) -> bool:
        """Whether the endpoint was named by a human.

        The gate on every auto-start/auto-restart in this class. See the module
        docstring."""
        return self.endpoint().explicit

    def discover(self) -> dict:
        """Return ``{"transport": "tcp", "url": ...}`` for the endpoint.

        Pure resolution — no probe, no subprocess. Whether anything is actually
        listening is :meth:`is_alive`'s question."""
        ep = self.endpoint()
        return {"transport": "tcp", "url": ep.url}

    # ---- connect probe + ws_url ----------------------------------------

    def is_alive(self) -> bool:
        """Cheap reachability check: does the endpoint answer ``/__ping__``?"""
        try:
            return self._ping()
        except OSError:
            return False

    def wait_until_alive(self, timeout: float = 8.0, interval: float = 0.2) -> bool:
        """Poll :meth:`is_alive` until the daemon answers or ``timeout`` passes.
        Used after a respawn so the caller doesn't race the new daemon's bind.
        Returns whether the daemon came up in time."""
        import time
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.invalidate()
            if self.is_alive():
                return True
            time.sleep(interval)
        return False

    def _ping(self) -> bool:
        """HTTP ``GET /__ping__`` against the endpoint.

        Deliberately not a CDP request: the upstream browser may not be open
        yet, and all we want to know is that the daemon's accept loop is live
        and is *ours* (the pong shape is what proves the second half)."""
        from .daemon import _ipc
        return _ipc.ping_status_sync(timeout=1.5).pid is not None

    def unreachable_error(self) -> DaemonUnavailable:
        """The error to raise when nothing answered an explicit endpoint."""
        return DaemonUnavailable(unreachable_message(self.endpoint()))

    def ws_url(self, *, client_label: Optional[str] = None) -> str:
        """Return the control-surface ws URL the ``CDPSession`` opens.

        Caches the result; call ``invalidate()`` to force a re-resolve (e.g.
        after a 1011 close).
        """
        if client_label is None:
            client_label = self._client_label
        if self._cached_ws:
            return self._cached_ws
        ep = self.endpoint()
        # Session-bound clients carry ``?session=<id>`` — the daemon dispatcher
        # routes on this (not on the client label). Without it a cdp session
        # resolves to None → the shared (extension) context.
        url = ep.ws("/control", client=client_label, session=self._session_id)
        self._cached_ws = url
        self._endpoint = ep.url
        self._transport = "tcp"
        return url

    def invalidate(self) -> None:
        self._cached_ws = None

    # Mode A / Mode B protocol alias — Session._resolve_ws_url() picks this.
    def resolve_ws_url(self) -> str:
        return self.ws_url()

    # ---- backend identity ----------------------------------------------

    def get_backend_info(self) -> Optional[dict]:
        """Return the running daemon's reported backend, or ``None`` if the
        daemon doesn't support the ``BrowserwrightDaemon.getBackendInfo`` RPC.
        Used by ``ensure_version_coherent`` to pin the backend on a respawn.

        We use the CLI shim ``browserwright-daemon backend-info --name <X>
        --json`` (zero-side-effect, mirrors doctor's contract) because that's
        the easiest path that doesn't require us to open a ws first.
        """
        try:
            cmd = ["browserwright-daemon", "backend-info", "--json"]
            if self._session_id:
                cmd += ["--session", self._session_id]
            proc = subprocess.run(
                cmd,
                capture_output=True, text=True, timeout=5,
                # The child must ask the SAME daemon we are talking to; a
                # `--daemon-url` flag does not propagate on its own (ADR-0011).
                env=child_env(),
            )
        except (FileNotFoundError, subprocess.TimeoutExpired):
            return None
        if proc.returncode != 0 or not proc.stdout.strip():
            return None
        try:
            return json.loads(proc.stdout)
        except json.JSONDecodeError:
            return None

    # ---- S6 (A2-a): daemon ↔ code version coherence --------------------
    #
    # A daemon that's been running across a package upgrade speaks the OLD
    # protocol — newer RPC methods come back as -32601 "unknown method", which
    # looks like a mysterious failure (the session-1 pothole). We detect the
    # version skew up front and restart the daemon so it picks up the new code.

    def running_daemon_version(self) -> Optional[str]:
        """Version the *running* daemon advertises on its ``/__ping__`` pong.

        Read straight off the endpoint rather than by shelling out to
        ``browserwright-daemon status --json``: a ``--daemon-url`` flag does not
        reach a child process's environment, so the subprocess would happily
        report the *local* daemon's version while we are talking to a remote one.

        Returns ``None`` when the daemon isn't reachable OR is too old to
        advertise a version. A missing version is deliberately indistinguishable
        from "no daemon" here; the coherence guard disambiguates via
        :meth:`is_alive`."""
        from .daemon import _ipc
        return _ipc.ping_status_sync(timeout=1.5).version

    def installed_daemon_version(self) -> Optional[str]:
        """Version of the ``browserwright-daemon`` package installed on disk, read
        from ``browserwright-daemon version``. ``None`` when it can't be determined
        (in which case the coherence guard declines to act — better than
        thrash-restarting on a comparison we can't make)."""
        try:
            proc = subprocess.run(
                ["browserwright-daemon", "version"],
                capture_output=True, text=True, timeout=3,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired):
            return None
        if proc.returncode != 0:
            return None
        # Output shape: "browserwright-daemon X.Y.Z" — take the last whitespace token.
        text = (proc.stdout or "").strip()
        if not text:
            return None
        return text.split()[-1] or None

    def _stop_daemon(self) -> None:
        """Stop the running daemon (mirrors the PID-guarded ``stop`` CLI)."""
        try:
            subprocess.run(
                ["browserwright-daemon", "stop"],
                capture_output=True, text=True, timeout=10,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired):
            pass

    def _spawn_daemon(self, backend: Optional[str] = None) -> None:
        """Spawn a fresh daemon. Detached so it outlives this process, mirroring
        how cold-start launches ``serve``.

        ``backend`` pins ``--backend`` on the respawn. The daemon refuses to
        start under auto (it would silently fall back to cdp and leave the
        extension relay un-bound), so a restart that drops the backend would
        kill the daemon. Callers that know the backend the old daemon was
        serving (see ``ensure_version_coherent``) pass it through so the
        replacement keeps serving the same backend."""
        if self.explicit:
            # ADR-0011: an endpoint someone configured is a daemon someone else
            # manages. Spawning a local one here would bind a *different*
            # address and silently serve the wrong browser.
            return
        cmd = ["browserwright-daemon", "serve"]
        if backend:
            cmd += ["--backend", backend]
        try:
            subprocess.Popen(
                cmd,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                stdin=subprocess.DEVNULL, start_new_session=True,
            )
        except FileNotFoundError:
            pass

    def ensure_version_coherent(self) -> bool:
        """If the running daemon's version differs from the installed package
        version (or it advertises none at all), stop + respawn it so the new
        code takes effect. Returns ``True`` iff a restart was performed.

        No-ops (returns ``False``) when:
          - there's no daemon running at all (cold-start owns spawning), or
          - the installed version can't be determined (can't compare safely).

        Generic by construction: it never inspects RPC methods or specific
        version strings — any future skew is handled the same way.

        ADR-0011: also a no-op against an **explicitly configured** endpoint. A
        remote daemon is not ours to stop, and `browserwright-daemon stop` here
        would signal whatever local daemon happens to exist instead — the wrong
        process, on the wrong machine. We warn and keep going; a real skew then
        surfaces as the usual actionable ``-32601`` message."""
        if self.explicit:
            import sys
            installed = self.installed_daemon_version()
            running = self.running_daemon_version()
            if installed and running and running != installed:
                print(
                    f"warning: the daemon at {self.endpoint().url} runs "
                    f"{running} but this client is {installed}. Because the "
                    "endpoint was configured explicitly, browserwright will "
                    "not restart it — restart it yourself on that machine.",
                    file=sys.stderr)
            return False
        installed = self.installed_daemon_version()
        if installed is None:
            return False
        running = self.running_daemon_version()
        if running is None:
            # Distinguish "no daemon" (don't touch) from "daemon too old to
            # report a version" (stale → restart).
            if not self.is_alive():
                return False
        elif running == installed:
            return False
        # running is None-but-alive (legacy) OR running != installed → stale.
        # Capture the backend the stale daemon is serving BEFORE we stop it, so
        # the respawn pins the same backend. The daemon refuses to start under
        # auto, so dropping the backend here would leave it dead. A daemon too
        # old to answer backend-info yields None → respawn without a pin and let
        # the daemon's own guard decide (BD_BACKEND/default_backend).
        prior = self.get_backend_info() or {}
        backend = prior.get("backend") or None
        self._stop_daemon()
        self._spawn_daemon(backend=backend)
        self.invalidate()
        return True

    # ---- S6 (A2-b): rewrite -32601 "unknown method" --------------------

    @staticmethod
    def is_stale_method_error(error: Any) -> bool:
        """True iff a JSON-RPC error object is a ``-32601`` "method not found".
        Generic — keys only on the standard code, never on a method name."""
        return isinstance(error, dict) and error.get("code") == -32601

    @staticmethod
    def explain_rpc_error(method: str, error: Any) -> str:
        """Turn a JSON-RPC error object into a human-actionable message.

        For ``-32601`` (unknown method) — the signature of a daemon running
        older code than what's installed — we rewrite the raw envelope into a
        clear "the daemon is stale, restart it" message that names the offending
        method. Any other code is surfaced as its own message verbatim (those
        are real protocol errors, not staleness)."""
        if ModeBClient.is_stale_method_error(error):
            return (
                f"the running daemon doesn't have method {method!r} — it is "
                f"likely stale (older than the installed code). The next "
                f"command against the default endpoint replaces a stale daemon "
                f"automatically; `browserwright version check` shows both "
                f"versions."
            )
        if isinstance(error, dict) and error.get("message"):
            return str(error["message"])
        return f"RPC {method!r} failed: {error!r}"


# ---- factory: build a client bound to a resolved session ------------

def client_for_session(record: dict) -> ModeBClient:
    """Build a Mode B client for the single global daemon (fixed socket).

    The connection carries the session identity as its client label
    (``skill-s<id>``) for daemon-side observability and per-session routing;
    falls back to the default ``skill-repl`` when the record has no id.

    Construction is lazy — ``DaemonUnavailable`` surfaces only when a primitive
    first resolves the ws — but when the daemon *is* already up we restart it if
    it's running stale code (S6 / A2-a), so we don't lean on newer RPCs against
    an old protocol."""
    client = ModeBClient()
    sid = record.get("id")
    if sid:
        client._client_label = f"skill-s{sid}"
        client._session_id = str(sid)
    if client.is_alive() and client.ensure_version_coherent():
        client.wait_until_alive()
    return client
