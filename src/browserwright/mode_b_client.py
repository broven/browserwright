"""Mode B daemon client — long-lived socket connection (spec §10 v0.2, §D).

Mode B is the v0.2 happy path:

  - Skill connects to a running ``browserwright-daemon serve`` instance via its
    unix-socket (POSIX) or TCP+token (Windows) endpoint.
  - Standard CDP commands are tunnelled through. ``BrowserwrightDaemon.*`` RPCs
    (``getActiveTab``, ``disconnect``, ``subscribeFocus``, ``uiState``) are
    answered by the daemon itself, not forwarded upstream.
  - Events fan out to the client: ``upstreamClosed``, ``activeTabChanged``,
    ``upstreamReady`` etc.

The Skill side here is a single-threaded sync wrapper that ``Session`` holds
as its sole daemon client (Mode A — the one-shot subprocess resolver — was
removed; the skill always talks to a running daemon over its socket).

Discovery:
  - Endpoint path comes from ``browserwright-daemon status --json`` (or directly
    ``${XDG_RUNTIME_DIR:-/tmp}/browserwright-daemon-${BD_NAME}.sock``).
  - On connect, the client appends ``?client=skill-repl`` to the URL.

The CDPSession transport connects to our Mode B unix endpoint (translated to
``ws+unix://``). :func:`client_for_session` builds the client from a resolved
ledger record; ``DaemonUnavailable`` surfaces lazily when no daemon socket is
reachable.
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import threading
from pathlib import Path
from typing import Any, Optional

from .errors import DaemonUnavailable


def _default_name() -> str:
    """Live ``BD_NAME`` lookup. NOT a module-level constant: freezing identity
    at import time was the silent cross-talk root (P1). Callers resolving a
    session pass an explicit endpoint via :func:`client_for_session` instead."""
    return os.environ.get("BD_NAME", "default")


def _default_socket_path(name: Optional[str] = None) -> Path:
    base = os.environ.get("XDG_RUNTIME_DIR") or "/tmp"
    return Path(base) / f"browserwright-daemon-{name or _default_name()}.sock"


def _windows_port_file(name: Optional[str] = None) -> Path:
    return Path(os.environ.get("TEMP", "/tmp")) / f"browserwright-daemon-{name or _default_name()}.port"


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
                ["browserwright-daemon", "status", "--name", self.name, "--json"],
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

    # ---- backend identity ----------------------------------------------

    def get_backend_info(self) -> Optional[dict]:
        """Return the running daemon's reported backend, or ``None`` if the
        daemon doesn't support the ``BrowserwrightDaemon.getBackendInfo`` RPC.
        Surfaced via ``Session.backend_name`` so primitives can branch on
        backend quirks (e.g. extension's explicit-attach model).

        We use the CLI shim ``browserwright-daemon backend-info --name <X>
        --json`` (zero-side-effect, mirrors doctor's contract) because that's
        the easiest path that doesn't require us to open a ws first.
        """
        try:
            proc = subprocess.run(
                ["browserwright-daemon", "backend-info",
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

    # ---- minimal one-shot RPC (subprocess CLI) -------------------------
    # These let a caller ask the *same* daemon for BrowserwrightDaemon.* answers
    # via its CLI subcommands without opening a ws. The interesting ones
    # (subscribeFocus, uiState) require a live ws and are handled inside
    # CDPSession instead.

    def active_tab(self) -> Optional[dict]:
        """Best-guess user-active tab via the ``browserwright-daemon active-tab``
        CLI subcommand; the ws-based ``BrowserwrightDaemon.getActiveTab`` RPC is
        wired into ``Session`` when a CDP connection is up."""
        try:
            proc = subprocess.run(
                ["browserwright-daemon", "active-tab", "--name", self.name, "--json"],
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
                ["browserwright-daemon", "attach-active",
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
                ["browserwright-daemon", "disconnect", "--name", self.name,
                 "--reason", reason],
                capture_output=True, text=True, timeout=5,
            )
            return proc.returncode == 0
        except (FileNotFoundError, subprocess.TimeoutExpired):
            return False

    # ---- Phase B: open_background / close_tab CLI shims ---------------

    def open_background(self, url: str, *, group: str = "Agent") -> Optional[dict]:
        """Phase B Feature 1 — invoke ``browserwright-daemon open-background``.

        Returns the parsed JSON result (``{sessionId,targetId,tabId,url,
        title,groupId}``) or ``None`` if the CLI was unavailable. On
        failure the captured subprocess detail is stashed on
        ``self.last_cli_error`` so the caller can surface a meaningful
        message instead of guessing. The daemon-side handler requires
        backend=extension; on any other backend the call surfaces an
        error (returncode != 0) which is recorded here verbatim.
        """
        self.last_cli_error = None
        cmd = ["browserwright-daemon", "open-background",
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
        """Phase B Feature 2 — invoke ``browserwright-daemon close-tab``.

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
        cmd = ["browserwright-daemon", "close-tab", "--name", self.name]
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
        """Forward ``browserwright-daemon doctor --json`` over a subprocess."""
        try:
            proc = subprocess.run(
                ["browserwright-daemon", "doctor", "--json"],
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

    # ---- S6 (A2-a): daemon ↔ code version coherence --------------------
    #
    # A daemon that's been running across a package upgrade speaks the OLD
    # protocol — newer RPC methods come back as -32601 "unknown method", which
    # looks like a mysterious failure (the session-1 pothole). We detect the
    # version skew up front and restart the daemon so it picks up the new code.

    def running_daemon_version(self) -> Optional[str]:
        """Version the *running* daemon advertises via ``status --json``.

        Returns ``None`` when the daemon isn't reachable OR is too old to
        advertise a version. A missing version is deliberately indistinguishable
        from "no daemon" here; the coherence guard disambiguates via
        :meth:`is_alive`."""
        try:
            proc = subprocess.run(
                ["browserwright-daemon", "status", "--name", self.name, "--json"],
                capture_output=True, text=True, timeout=3,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired):
            return None
        if proc.returncode != 0 or not proc.stdout.strip():
            return None
        try:
            info = json.loads(proc.stdout)
        except json.JSONDecodeError:
            return None
        v = info.get("version")
        return v if isinstance(v, str) and v else None

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
                ["browserwright-daemon", "stop", "--name", self.name],
                capture_output=True, text=True, timeout=10,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired):
            pass

    def _spawn_daemon(self, backend: Optional[str] = None) -> None:
        """Spawn a fresh daemon. Detached so it outlives this process, mirroring
        how cold-start launches ``serve``.

        ``backend`` pins ``--backend`` on the respawn. The daemon refuses to
        start under auto (it would silently fall back to rdp and leave the
        extension relay un-bound), so a restart that drops the backend would
        kill the daemon. Callers that know the backend the old daemon was
        serving (see ``ensure_version_coherent``) pass it through so the
        replacement keeps serving the same backend."""
        cmd = ["browserwright-daemon", "serve", "--name", self.name]
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
        version strings — any future skew is handled the same way."""
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
                f"likely stale (older than the installed code). Restart it with "
                f"`browserwright-daemon stop && browserwright-daemon serve`."
            )
        if isinstance(error, dict) and error.get("message"):
            return str(error["message"])
        return f"RPC {method!r} failed: {error!r}"


# ---- factory: build a client bound to a resolved session ------------

def client_for_session(record: dict) -> ModeBClient:
    """Build a Mode B client whose endpoint comes from the session *record*
    (P1), not the import-time default. ``record["daemon_endpoint"]`` is the
    daemon name/socket this session is bound to.

    The connection carries the session identity as its client label
    (``skill-s<id>``) for daemon-side observability; falls back to the default
    ``skill-repl`` when the record has no id.

    Construction is lazy — ``DaemonUnavailable`` surfaces only when a primitive
    first resolves the ws — but when the daemon *is* already up we restart it if
    it's running stale code (S6 / A2-a), so we don't lean on newer RPCs against
    an old protocol. There is no backend guessing: the backend is whatever the
    session's daemon was started to serve."""
    client = ModeBClient(name=record["daemon_endpoint"])
    sid = record.get("id")
    if sid:
        client._client_label = f"skill-s{sid}"
    if client.is_alive() and client.ensure_version_coherent():
        client.wait_until_alive()
    return client
