"""Mode B daemon client — long-lived socket connection (spec §10 v0.2, §D).

Mode B is the v0.2 happy path:

  - Skill connects to a running ``browser-daemon serve`` instance via its
    unix-socket (POSIX) or TCP+token (Windows) endpoint.
  - Standard CDP commands are tunnelled through. ``BrowserDaemon.*`` RPCs
    (``getActiveTab``, ``disconnect``, ``subscribeFocus``, ``uiState``) are
    answered by the daemon itself, not forwarded upstream.
  - Events fan out to the client: ``upstreamClosed``, ``activeTabChanged``,
    ``upstreamReady`` etc.

The Skill side here is a single-threaded sync wrapper, mirroring the
``ModeAClient`` shape so ``Session`` can hold either via a duck-typed
``DaemonClient`` protocol.

Discovery:
  - Endpoint path comes from ``browser-daemon status --json`` (or directly
    ``${XDG_RUNTIME_DIR:-/tmp}/browser-daemon-${BD_NAME}.sock``).
  - On connect, the client appends ``?client=skill-repl`` to the URL.

Auto mode (``DaemonClient`` factory): try Mode B socket → fall back to
Mode A subprocess. The CDPSession transport accepts either a real ws URL
(Mode A) or our Mode B unix endpoint (translated to ``ws+unix://``).
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import threading
from pathlib import Path
from typing import Any, Optional

from .errors import DaemonBackendMismatch, DaemonUnavailable


def _default_name() -> str:
    """Live ``BD_NAME`` lookup. NOT a module-level constant: freezing identity
    at import time was the silent cross-talk root (P1). Callers resolving a
    session pass an explicit endpoint via :func:`client_for_session` instead."""
    return os.environ.get("BD_NAME", "default")


def _default_socket_path(name: Optional[str] = None) -> Path:
    base = os.environ.get("XDG_RUNTIME_DIR") or "/tmp"
    return Path(base) / f"browser-daemon-{name or _default_name()}.sock"


def _windows_port_file(name: Optional[str] = None) -> Path:
    return Path(os.environ.get("TEMP", "/tmp")) / f"browser-daemon-{name or _default_name()}.port"


class ModeBClient:
    """Mode B daemon endpoint. Use ``connect()`` to confirm reachability;
    ``ws_url()`` returns the CDP-compatible URL Skill's ``CDPSession`` can
    open. Active-tab / disconnect / uiState are sent over the same socket.
    """

    def __init__(self, *, name: Optional[str] = None):
        self.name = name or _default_name()
        self._endpoint: Optional[str] = None
        self._transport: Optional[str] = None  # "unix" or "tcp"
        self._token: Optional[str] = None
        self._cached_ws: Optional[str] = None
        # client label sent on the ws query string for daemon observability;
        # session-bound clients override this with ``skill-s<id>``.
        self._client_label: str = "skill-repl"

    # ---- endpoint discovery ---------------------------------------------

    def discover(self) -> dict:
        """Return ``{"transport": ..., "path": ..., "host": ..., "port": ...,
        "token": ...}``. Probes the daemon's ``status --json`` first; falls
        back to direct path inspection on POSIX so we still work when
        daemon CLI is on a slow path."""
        try:
            proc = subprocess.run(
                ["browser-daemon", "status", "--name", self.name, "--json"],
                capture_output=True, text=True, timeout=3,
            )
            if proc.returncode == 0 and proc.stdout.strip():
                info = json.loads(proc.stdout)
                if info.get("alive"):
                    return self._normalize_endpoint_info(info)
        except (FileNotFoundError, subprocess.TimeoutExpired, json.JSONDecodeError):
            pass

        # POSIX fallback: just look at the well-known socket path.
        if os.name != "nt":
            sock_path = _default_socket_path(self.name)
            if sock_path.exists():
                return {"transport": "unix", "path": str(sock_path)}

        # Windows fallback: look at the port file.
        port_file = _windows_port_file(self.name)
        if port_file.exists():
            try:
                data = json.loads(port_file.read_text(encoding="utf-8"))
                if "port" in data and "token" in data:
                    return {
                        "transport": "tcp",
                        "host": data.get("host", "127.0.0.1"),
                        "port": int(data["port"]),
                        "token": data["token"],
                    }
            except (OSError, ValueError):
                pass
        raise DaemonUnavailable(f"no Mode B endpoint for BD_NAME={self.name!r}")

    @staticmethod
    def _normalize_endpoint_info(info: dict) -> dict:
        # `status --json` may nest the transport details or flatten them; be
        # tolerant of both shapes daemon-implementer may ship.
        out = dict(info)
        if "endpoint" in info and isinstance(info["endpoint"], dict):
            out.update(info["endpoint"])
        out.pop("alive", None)
        # Drop everything outside our known schema so callers don't pin on it.
        return {k: out[k] for k in ("transport", "path", "host", "port", "token", "name")
                if k in out}

    # ---- connect probe + ws_url ----------------------------------------

    def is_alive(self) -> bool:
        """Cheap reachability check. Returns True iff the daemon's socket
        accepts a `ping`-style request."""
        try:
            ep = self.discover()
        except DaemonUnavailable:
            return False
        try:
            return self._ping(ep)
        except OSError:
            return False

    def _ping(self, ep: dict) -> bool:
        """Open a short-lived raw socket to the daemon endpoint and verify
        it's responsive. We avoid a CDP request because the upstream may
        not be open yet — we just want to know the daemon's accept loop is
        live."""
        if ep["transport"] == "unix":
            s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            s.settimeout(1.5)
            try:
                s.connect(ep["path"])
            except OSError:
                s.close()
                return False
            s.close()
            return True
        # TCP / windows
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(1.5)
        try:
            s.connect((ep.get("host", "127.0.0.1"), int(ep["port"])))
        except OSError:
            s.close()
            return False
        s.close()
        return True

    def ws_url(self, *, client_label: Optional[str] = None) -> str:
        """Return a ``ws+unix://`` or ``ws://`` URL the ``CDPSession`` can open.

        Caches the result; call ``invalidate()`` to force a re-resolve (e.g.
        after a 1011 close).
        """
        if client_label is None:
            client_label = self._client_label
        if self._cached_ws:
            return self._cached_ws
        ep = self.discover()
        if ep["transport"] == "unix":
            # websockets.sync.client.connect doesn't support ws+unix:// natively;
            # we hand it a pre-built socket via the `sock=` kwarg instead.
            # Return a sentinel URL the CDPSession layer recognises.
            url = f"ws+unix://{ep['path']}?client={client_label}"
        else:
            tok = ep.get("token", "")
            host = ep.get("host", "127.0.0.1")
            port = ep["port"]
            url = f"ws://{host}:{port}?token={tok}&client={client_label}"
        self._cached_ws = url
        self._endpoint = ep.get("path") or f"{ep.get('host')}:{ep.get('port')}"
        self._transport = ep["transport"]
        self._token = ep.get("token")
        return url

    def invalidate(self) -> None:
        self._cached_ws = None

    # Mode A / Mode B protocol alias — Session._resolve_ws_url() picks this.
    def resolve_ws_url(self) -> str:
        return self.ws_url()

    # ---- backend identity (F-5d) ---------------------------------------

    def get_backend_info(self) -> Optional[dict]:
        """Return the running daemon's reported backend, or ``None`` if
        the daemon doesn't support the ``BrowserDaemon.getBackendInfo``
        RPC. Used by ``assert_backend_matches()`` to refuse silently
        reusing a daemon configured for a different backend.

        Mode B's daemon supports this RPC over the same socket. We use
        the CLI shim ``browser-daemon backend-info --name <X> --json``
        when available (zero-side-effect, mirrors doctor's contract)
        because that's the easiest path that doesn't require us to open
        a ws first.
        """
        try:
            proc = subprocess.run(
                ["browser-daemon", "backend-info",
                 "--name", self.name, "--json"],
                capture_output=True, text=True, timeout=5,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired):
            return None
        if proc.returncode != 0 or not proc.stdout.strip():
            return None
        try:
            return json.loads(proc.stdout)
        except json.JSONDecodeError:
            return None

    def assert_backend_matches(self, expected: str) -> None:
        """Refuse to proceed if the running daemon's backend differs from
        ``expected``. Skipped silently when:
          - ``expected`` is empty / None (caller didn't pin a backend)
          - The daemon doesn't expose ``backend-info`` (older daemon —
            we can't verify; fall through to the original behaviour)
          - ``BS_SKIP_BACKEND_IDENTITY_CHECK=1`` is set (escape hatch
            for explicit re-use, e.g. mid-test cross-backend probing)

        Raises ``DaemonBackendMismatch`` otherwise.
        """
        if not expected:
            return
        if os.environ.get(
                "BS_SKIP_BACKEND_IDENTITY_CHECK", "").lower() in {
                    "1", "true", "yes"}:
            return
        info = self.get_backend_info()
        if info is None:
            return
        actual = info.get("backend") or info.get("name")
        if not actual or actual == expected:
            return
        raise DaemonBackendMismatch(requested=expected, actual=actual,
                                    name=self.name)

    # ---- minimal one-shot RPC (subprocess CLI fallback) ----------------
    # These exist so callers that already have a CDPSession via Mode A can
    # still ask the *same* daemon for BrowserDaemon.* answers via its CLI
    # subcommands. The interesting ones (subscribeFocus, uiState) require a
    # live ws and are handled inside CDPSession instead.

    def active_tab(self) -> Optional[dict]:
        """Same shape as ``ModeAClient.active_tab`` — Mode B uses the same CLI
        subcommand here for now; the ws-based ``BrowserDaemon.getActiveTab``
        RPC is wired into ``Session`` when a Mode B CDP connection is up."""
        try:
            proc = subprocess.run(
                ["browser-daemon", "active-tab", "--name", self.name, "--json"],
                capture_output=True, text=True, timeout=8,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired):
            return None
        if proc.returncode != 0 or not proc.stdout.strip():
            return None
        try:
            data = json.loads(proc.stdout)
        except json.JSONDecodeError:
            return None
        return {
            "targetId": data.get("targetId"),
            "url": data.get("url", ""),
            "title": data.get("title", ""),
            "accuracy": data.get("accuracy", "unknown"),
            "since_seconds": data.get("since_seconds"),
        }

    def attach_active(self) -> Optional[dict]:
        """v0.5.4: ask the daemon's extension backend to attach the
        currently-focused-window active tab — bypasses the popup click.

        Returns ``{sessionId, targetId, tabId, url, title}`` on success,
        ``None`` if the daemon errored or isn't reachable. Only meaningful
        when the running daemon was started with ``--backend extension``;
        other backends will return -32601 ("requires the extension backend")
        and that surfaces as ``None`` here.
        """
        try:
            proc = subprocess.run(
                ["browser-daemon", "attach-active",
                 "--name", self.name, "--json"],
                capture_output=True, text=True, timeout=20,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired):
            return None
        if proc.returncode != 0 or not proc.stdout.strip():
            return None
        try:
            return json.loads(proc.stdout)
        except json.JSONDecodeError:
            return None

    def disconnect_upstream(self, reason: str = "skill_idle") -> bool:
        """Ask the daemon to close its upstream ws (banner disappears) but
        keep our socket alive. Used by REPL idle policy."""
        try:
            proc = subprocess.run(
                ["browser-daemon", "disconnect", "--name", self.name,
                 "--reason", reason],
                capture_output=True, text=True, timeout=5,
            )
            return proc.returncode == 0
        except (FileNotFoundError, subprocess.TimeoutExpired):
            return False

    # ---- Phase B: open_background / close_tab CLI shims ---------------

    def open_background(self, url: str, *, group: str = "Agent") -> Optional[dict]:
        """Phase B Feature 1 — invoke ``browser-daemon open-background``.

        Returns the parsed JSON result (``{sessionId,targetId,tabId,url,
        title,groupId}``) or ``None`` if the CLI was unavailable. On
        failure the captured subprocess detail is stashed on
        ``self.last_cli_error`` so the caller can surface a meaningful
        message instead of guessing. The daemon-side handler requires
        backend=extension; on any other backend the call surfaces an
        error (returncode != 0) which is recorded here verbatim.
        """
        self.last_cli_error = None
        cmd = ["browser-daemon", "open-background",
               "--name", self.name,
               "--url", url,
               "--group", group]
        try:
            proc = subprocess.run(
                cmd, capture_output=True, text=True, timeout=15,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired) as e:
            self.last_cli_error = f"subprocess failed: {e!r}"
            return None
        if proc.returncode != 0 or not proc.stdout.strip():
            self.last_cli_error = (
                f"`{' '.join(cmd)}` exit={proc.returncode}; "
                f"stderr={proc.stderr.strip() or '<empty>'}; "
                f"stdout={proc.stdout.strip() or '<empty>'}"
            )
            return None
        try:
            return json.loads(proc.stdout)
        except json.JSONDecodeError:
            self.last_cli_error = (
                f"`{' '.join(cmd)}` returned non-JSON stdout: {proc.stdout!r}"
            )
            return None

    def close_tab(
        self, session_id: str | None = None, *, target_id: str | None = None,
    ) -> Optional[dict]:
        """Phase B Feature 2 — invoke ``browser-daemon close-tab``.

        Pass ``target_id`` (the ``ext-tab-N`` string returned by
        ``open_background``) when calling from a fresh subprocess context —
        the CLI's transient ws can't see other clients' session bindings.
        ``session_id`` works only from a persistent ws (e.g. inside the
        Skill REPL where the same client connection issued the open).

        Returns ``{"ok":True,"tabId":N}`` on success or ``None`` when the
        CLI is unreachable / the daemon errored.
        """
        if not session_id and not target_id:
            return None
        self.last_cli_error = None
        cmd = ["browser-daemon", "close-tab", "--name", self.name]
        if target_id:
            cmd += ["--target-id", target_id]
        if session_id:
            cmd += ["--session-id", session_id]
        try:
            proc = subprocess.run(
                cmd, capture_output=True, text=True, timeout=10,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired) as e:
            self.last_cli_error = f"subprocess failed: {e!r}"
            return None
        if proc.returncode != 0 or not proc.stdout.strip():
            self.last_cli_error = (
                f"`{' '.join(cmd)}` exit={proc.returncode}; "
                f"stderr={proc.stderr.strip() or '<empty>'}; "
                f"stdout={proc.stdout.strip() or '<empty>'}"
            )
            return None
        try:
            return json.loads(proc.stdout)
        except json.JSONDecodeError:
            self.last_cli_error = (
                f"`{' '.join(cmd)}` returned non-JSON stdout: {proc.stdout!r}"
            )
            return None

    def doctor(self) -> dict:
        """For parity with ``ModeAClient.doctor``."""
        try:
            proc = subprocess.run(
                ["browser-daemon", "doctor", "--json"],
                capture_output=True, text=True, timeout=10,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired) as e:
            return {"schema_version": 1, "backends": [], "error": str(e),
                    "skill_synthetic": True}
        if proc.returncode != 0:
            return {"schema_version": 1, "backends": [],
                    "error": (proc.stderr or proc.stdout).strip(),
                    "skill_synthetic": True}
        try:
            return json.loads(proc.stdout)
        except json.JSONDecodeError:
            return {"schema_version": 1, "backends": [],
                    "error": "doctor output was not JSON",
                    "skill_synthetic": True}


# ---- factory: build a client bound to a resolved session ------------

def client_for_session(record: dict) -> ModeBClient:
    """Build a Mode B client whose endpoint comes from the session *record*
    (P1), not the import-time default. ``record["daemon_endpoint"]`` is the
    daemon name/socket this session is bound to.

    The connection carries the session identity as its client label
    (``skill-s<id>``) for daemon-side observability; falls back to the default
    ``skill-repl`` when the record has no id."""
    client = ModeBClient(name=record["daemon_endpoint"])
    sid = record.get("id")
    if sid:
        client._client_label = f"skill-s{sid}"
    return client


# ---- factory: auto-pick Mode B → Mode A ------------------------------

def auto_client(mode: Optional[str] = None, *, backend: Optional[str] = None):
    """Return whichever client matches ``mode`` (or the resolved default).

    ``mode`` values:
      - ``"A"`` — force ``ModeAClient``
      - ``"B"`` — force ``ModeBClient`` (raises DaemonUnavailable if no socket)
      - ``"auto"`` / None — try Mode B; fall back to Mode A on miss.

    Env override ``BS_DAEMON_MODE`` takes precedence over the argument.
    """
    from .daemon_client import DaemonClient as ModeAClient

    mode = (os.environ.get("BS_DAEMON_MODE") or mode or "auto").upper()
    requested_backend = backend or os.environ.get("BS_DAEMON_BACKEND") \
        or os.environ.get("BD_BACKEND")
    if mode == "A":
        return ModeAClient(backend=backend)
    if mode == "B":
        mb = ModeBClient()
        if not mb.is_alive():
            raise DaemonUnavailable(
                f"Mode B socket not reachable (BD_NAME={mb.name!r}); "
                f"start it with `browser-daemon serve` or use --mode=auto.")
        # F-5d: refuse silent reuse of a daemon serving a different backend.
        mb.assert_backend_matches(requested_backend or "")
        return mb
    # auto
    mb = ModeBClient()
    if mb.is_alive():
        mb.assert_backend_matches(requested_backend or "")
        return mb
    return ModeAClient(backend=backend)
