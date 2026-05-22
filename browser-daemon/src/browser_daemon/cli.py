"""argparse + subcommand dispatch.

Spec §5: Skill talks via subprocess and parses stdout/stderr/exit codes.
Stdout discipline (every subcommand):
- Success: one well-defined payload line, no decoration.
- Failure: stderr gets 1-3 human lines; stdout stays empty.

Exit codes (§5.1):
  0 success
  1 user error (bad CLI args, unknown backend)
  2 backend(s) unavailable
  3 internal / unexpected error
  6 launch-chrome: Chrome binary not found
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
from xml.sax.saxutils import escape as _xml_escape
from typing import NoReturn

from . import __version__
from .backends import names
from .config import load, Config
from .errors import ChromeBinaryNotFound, DaemonError, Unavailable, UserError


# ---- entrypoint ------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    handler = _DISPATCH.get(args.cmd)
    if handler is None:
        parser.print_help(sys.stderr)
        return 1
    try:
        cfg = _cfg_from_args(args)
        return handler(args, cfg)
    except UserError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    except ChromeBinaryNotFound as e:
        print(f"error: {e}", file=sys.stderr)
        return 6
    except Unavailable as e:
        print(f"error: {e}", file=sys.stderr)
        if e.attempts and getattr(args, "verbose", False):
            for name, why in e.attempts.items():
                print(f"  {name}: {why}", file=sys.stderr)
        return 2
    except DaemonError as e:
        # Catch-all daemon-internal failure that isn't UserError or Unavailable.
        print(f"error: {e}", file=sys.stderr)
        return 3
    except KeyboardInterrupt:
        return 130
    except Exception as e:  # truly unexpected
        print(f"internal error: {type(e).__name__}: {e}", file=sys.stderr)
        return 3


def _entry() -> NoReturn:
    sys.exit(main())


# ---- parser ----------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="browser-daemon",
        description=(
            "Resolve a browser-level CDP WebSocket from any local Chrome. "
            "v0.1 is Mode A only (one-shot CLI). Mode B socket arrives in v0.2."
        ),
    )
    sub = p.add_subparsers(dest="cmd", metavar="<subcommand>")

    # url
    p_url = sub.add_parser("url", help="resolve a CDP ws URL and print it")
    _add_common(p_url)
    _add_port(p_url)
    p_url.add_argument("--json", action="store_true", help="emit a JSON object instead of a bare URL")
    p_url.add_argument("--mode-b-proxy", action="store_true",
                       help="instead of upstream ws, output the daemon socket endpoint (v0.2)")
    _add_name(p_url)

    # serve (v0.2)
    p_serve = sub.add_parser("serve", help="run the long-lived Mode B daemon (v0.2)")
    _add_common(p_serve)
    _add_port(p_serve)
    _add_name(p_serve)
    # v0.5.3 Task #24: extension relay port override. Useful when default
    # 19989 is occupied (e.g., a stale daemon process). playwriter sits on
    # 19988, so the default no longer collides with it.
    # Precedence: this flag > BD_EXTENSION_PORT > toml > 19989 default.
    p_serve.add_argument(
        "--extension-port", type=int, default=None, metavar="N",
        help=("Bind the extension relay ws server on this port instead of "
              "the default 19989. Only relevant when --backend extension. "
              "Equivalent to BD_EXTENSION_PORT env or "
              "[backends.extension].port in config.toml."))

    # stop (v0.2)
    p_stop = sub.add_parser("stop", help="stop a running daemon (by BD_NAME)")
    _add_name(p_stop)
    p_stop.add_argument("--timeout", type=float, default=5.0,
                        help="seconds to wait for graceful shutdown before SIGKILL")

    # status (v0.2)
    p_status = sub.add_parser("status", help="report the daemon's IPC endpoint + liveness")
    _add_name(p_status)
    p_status.add_argument("--json", action="store_true")

    # disconnect (v0.2 §6.6)
    p_disc = sub.add_parser("disconnect",
        help="ask the running daemon to close its upstream ws (banner goes away)")
    _add_name(p_disc)
    p_disc.add_argument("--reason", default="skill_disconnect",
                        help="reason string surfaced in upstreamClosed event")

    # logs (v0.2)
    p_logs = sub.add_parser("logs", help="print the daemon log file path or tail it")
    _add_name(p_logs)
    p_logs.add_argument("--follow", "-f", action="store_true", help="tail -f the log")

    # doctor
    p_doc = sub.add_parser("doctor", help="probe all backends (zero ws side effects by default)")
    _add_common(p_doc)
    p_doc.add_argument("--probe-ws", action="store_true",
                       help="opt-in: actually open a ws on each available backend (NOT IMPLEMENTED in v0.1)")
    p_doc.add_argument("--json", action="store_true", help="emit JSON instead of pretty text")

    # list-backends
    p_lb = sub.add_parser("list-backends", help="enumerate backends statically (no probe)")
    _add_common(p_lb)
    p_lb.add_argument("--json", action="store_true")

    # active-tab
    p_at = sub.add_parser("active-tab", help="best-guess user-active tab (heuristic, opens ws)")
    _add_common(p_at)
    _add_port(p_at)
    p_at.add_argument("--json", action="store_true")

    # backend-info — what backend is the running daemon serving?
    # Mode B Skill clients shell out to this to decide whether the current
    # daemon matches their expected backend (refused-mismatch guard) and to
    # branch primitives on backend-specific quirks (e.g. extension's "0
    # attached tabs is actionable, not empty Chrome").
    p_bi = sub.add_parser(
        "backend-info",
        help="report the running daemon's backend identity (Mode B identity probe)")
    _add_name(p_bi)
    p_bi.add_argument("--json", action="store_true")

    # attach-active (v0.5.4 — extension backend only)
    p_aa = sub.add_parser(
        "attach-active",
        help="(extension backend) attach the focused-window active tab without a popup click")
    _add_name(p_aa)
    p_aa.add_argument("--json", action="store_true")

    # launch-chrome
    p_lc = sub.add_parser("launch-chrome", help="launch a detached isolated-profile Chrome and print its ws URL")
    p_lc.add_argument("--profile", default="isolated")
    g = p_lc.add_mutually_exclusive_group()
    g.add_argument("--persistent", action="store_true", default=True,
                   help="reuse a profile dir across launches (default)")
    g.add_argument("--tmp", action="store_true", default=False,
                   help="allocate a fresh tmpdir per launch (caller cleans it up)")
    p_lc.add_argument("--chrome-binary", help="absolute path to chrome / chromium / msedge / brave")
    p_lc.add_argument("--port", type=int, default=None,
                      help="--remote-debugging-port=N; default 0 (OS-picked)")
    # v0.5.3 F-18: `--detach` flag was reserved-in-v0.1 placeholder for a
    # non-detach mode we never shipped + never needed (Chrome always
    # detaches via `_spawn_kwargs()`). Removed to declutter the help text;
    # if a future need arises, re-add with real behavior.
    p_lc.add_argument("--timeout", type=float, default=30.0)
    p_lc.add_argument("--json", action="store_true")
    p_lc.add_argument("-v", "--verbose", action="store_true")
    # v0.5 Task #11 expert escape: bypass the "refuse user-default profile"
    # guard. STRONGLY discouraged — see chrome-popup-accumulation-bug memory.
    p_lc.add_argument(
        "--allow-default-profile", action="store_true", default=False,
        help=(
            "EXPERT ESCAPE HATCH: allow launch-chrome to target the user's "
            "default Chrome profile. Doing so permanently taints the daily "
            "Chrome with --remote-debugging-port; every ws upgrade triggers "
            "a Chrome 'Allow remote debugging?' popup. Equivalent to env "
            "BD_LAUNCH_CHROME_ALLOW_DEFAULT_PROFILE=1."))

    # version
    sub.add_parser("version", help="print the installed version and exit")

    # stats (v0.5 observability)
    p_stats = sub.add_parser("stats", help="dump in-process metrics counters")
    _add_name(p_stats)
    p_stats.add_argument("--json", action="store_true",
                         help="emit as JSON (default: tab-separated)")

    # open-background (Phase B — extension backend only)
    p_ob = sub.add_parser(
        "open-background",
        help=("open a Chrome tab in the background (group=Agent by default), "
              "attach chrome.debugger, print {sessionId,targetId,tabId,url,title,groupId}"),
    )
    _add_name(p_ob)
    p_ob.add_argument("--url", required=True, help="URL to open in the background tab")
    p_ob.add_argument("--group", default="Agent",
                      help="Chrome tab-group title to place the new tab in (default: Agent)")
    # Output is always JSON (spec §5.1 single-line discipline); no --json flag.

    # close-tab (Phase B — extension backend only)
    p_ct = sub.add_parser(
        "close-tab",
        help="close a tab by sessionId (persistent ws) or targetId (CLI)",
    )
    _add_name(p_ct)
    p_ct.add_argument("--session-id", default=None,
                      help="local sessionId from a persistent ws (Skill REPL)")
    p_ct.add_argument("--target-id", default=None,
                      help="globally-addressable targetId (e.g. ext-tab-42); "
                           "use this from one-shot CLI calls since transient "
                           "ws can't share per-client session state")
    # Output is always JSON (spec §5.1 single-line discipline); no --json flag.

    # end-session (P5 — extension backend only)
    p_es = sub.add_parser(
        "end-session",
        help="tear down a browser-skill session's tabs: close owned, keep borrowed",
    )
    _add_name(p_es)
    p_es.add_argument("--session", required=True,
                      help="the browser-skill session id whose tabs to clean up")
    p_es.add_argument("--group-name", default=None,
                      help="durable tab-group title for session-reconnect "
                           "recovery: when the daemon lost this session's "
                           "owned-tab tracking (reconnect / restart), close "
                           "the tabs in this group instead")
    # Output is always JSON (single-line discipline); no --json flag.

    # userscript — resident extension userscripts
    p_us = sub.add_parser("userscript", help="manage resident extension userscripts")
    _add_name(p_us)
    us_sub = p_us.add_subparsers(dest="userscript_cmd", metavar="<action>")
    p_us_push = us_sub.add_parser("push", help="install or update a .user.js file")
    p_us_push.add_argument("file", help=".user.js path, or - for stdin")
    p_us_install = us_sub.add_parser("install", help="alias for push")
    p_us_install.add_argument("file", help=".user.js path, or - for stdin")
    p_us_list = us_sub.add_parser("list", help="list resident userscripts")
    p_us_list.add_argument("--site", default=None, help="filter by matching site URL")
    p_us_remove = us_sub.add_parser("remove", help="remove by identity or id")
    p_us_remove.add_argument("key", help="identity or id")
    p_us_toggle = us_sub.add_parser("toggle", help="enable or disable by identity or id")
    p_us_toggle.add_argument("key", help="identity or id")
    p_us_toggle.add_argument("--enabled", required=True, help="true or false")
    p_us_logs = us_sub.add_parser("logs", help="print userscript injection logs")
    p_us_logs.add_argument("--id", default=None, help="filter by userscript id")
    p_us_logs.add_argument("--limit", type=int, default=50, help="max log rows")

    # install / uninstall / list — long-running service (macOS LaunchAgent).
    # The daemon was designed as a one-shot `serve` subprocess, but for the
    # "zero manual ops after install" extension flow it needs to be a
    # supervised background service: starts at login, restarts on crash,
    # and is reachable on the same socket across reboots.
    p_inst = sub.add_parser(
        "install",
        help="register the daemon as a macOS LaunchAgent (auto-start + KeepAlive)")
    _add_name(p_inst)
    p_inst.add_argument("--backend", default="extension",
                        choices=names(),
                        help="backend the LaunchAgent will serve (default: extension)")
    p_inst.add_argument("--extension-port", type=int, default=None, metavar="N",
                        help="override the relay ws port (default 19989)")
    p_inst.add_argument("--force", action="store_true",
                        help="replace an existing LaunchAgent with the same name")

    p_uninst = sub.add_parser(
        "uninstall",
        help="remove the LaunchAgent (stops auto-start)")
    _add_name(p_uninst)

    p_ls = sub.add_parser(
        "list",
        help="enumerate installed LaunchAgents + running daemon instances")
    p_ls.add_argument("--json", action="store_true",
                      help="emit JSON instead of pretty text")

    return p


def _add_common(sp: argparse.ArgumentParser) -> None:
    sp.add_argument("--backend", choices=names(),
                    help="pin to one backend; otherwise the chain runs env -> rdp "
                         "(extension and cloud require explicit --backend)")
    sp.add_argument("--timeout", type=float, default=None,
                    help="per-backend timeout in seconds (default 5)")
    sp.add_argument("--config", help="optional toml config path; otherwise reads BD_CONFIG")
    sp.add_argument("-v", "--verbose", action="store_true")


def _add_port(sp: argparse.ArgumentParser) -> None:
    sp.add_argument("--port", type=int, default=None,
                    help="rdp backend port (default 9222 / config-backends.rdp.port)")


def _add_name(sp: argparse.ArgumentParser) -> None:
    sp.add_argument("--name", default=None,
                    help="daemon instance name for multi-instance setups; "
                         "overrides BD_NAME (default 'default')")


# ---- shared config building ------------------------------------------------


def _cfg_from_args(args) -> Config:
    return load(
        cli_backend=getattr(args, "backend", None),
        cli_timeout=getattr(args, "timeout", None),
        cli_port=getattr(args, "port", None),
        cli_chrome_binary=getattr(args, "chrome_binary", None),
        cli_config_path=getattr(args, "config", None),
        cli_name=getattr(args, "name", None),
        # v0.5.3 Task #24: serve-only flag; argparse Namespace shape varies
        # per subcommand, so getattr-with-default keeps non-serve calls clean.
        cli_extension_port=getattr(args, "extension_port", None),
    )


def _run(coro):
    return asyncio.run(coro)


# ---- subcommand handlers ---------------------------------------------------


def _cmd_url(args, cfg: Config) -> int:
    # --mode-b-proxy → output the daemon socket endpoint, not an upstream URL.
    # (Spec §6.1: bare socket path on POSIX, host:port + token on Windows.)
    if getattr(args, "mode_b_proxy", False):
        from . import _ipc
        ep = _ipc.endpoint_describe(cfg.name)
        if args.json:
            print(json.dumps(ep, sort_keys=True))
        else:
            if ep["transport"] == "unix":
                print(ep["path"])
            else:
                # `host:port token=...` so a one-liner shell consumer can split.
                token = ep["token"] or ""
                if ep["port"] is None:
                    # Daemon not running. Exit 2 so Skill can react.
                    print("error: no daemon running (no port file)", file=sys.stderr)
                    return 2
                print(f"{ep['host']}:{ep['port']} token={token}")
        return 0

    from .resolver import resolve

    rr = _run(resolve(cfg))
    if args.json:
        print(json.dumps({
            "schema_version": 1,
            "ws_url": rr.ws_url,
            "backend": rr.backend,
            "extras": rr.extras,
        }, sort_keys=True))
    else:
        # Bare URL — spec §5.1 stdout discipline: ONE line, no decoration.
        print(rr.ws_url)
    return 0


def _cmd_serve(args, cfg: Config) -> int:
    """Run the long-lived Mode B daemon (§5 v0.2).

    Unlike the Mode A probes (`url`, `doctor`), `serve` refuses to start under
    auto. The daemon binds its backend for its whole lifetime — an accidental
    auto→rdp fallback (e.g. a version-coherence respawn that dropped --backend)
    silently leaves the extension relay un-bound and the extension unable to
    connect. So require an explicit backend from CLI --backend, BD_BACKEND, or
    config.toml `default_backend` (all three feed cfg.backend); fail loud
    otherwise instead of guessing.
    """
    if cfg.backend is None:
        raise UserError(
            "serve requires an explicit backend (auto-selection is disabled "
            "for the long-lived daemon to avoid a silent rdp fallback). Pass "
            "--backend {env|rdp|extension|cloud}, or set BD_BACKEND, or "
            "`default_backend` in config.toml.")
    from .server.listener import run_serve
    return _run(run_serve(cfg))


def _cmd_stop(args, cfg: Config) -> int:
    """Send SIGTERM to a running daemon, wait briefly, fall back to SIGKILL.

    We do NOT trust the pid file alone — we ping first to verify it's our
    daemon, then signal that pid. (Mirrors browser-harness `_ipc.identify`.)

    PID-reuse guard: between the ping and the kill the daemon could die and the
    OS recycle its pid for an unrelated process. We fingerprint the pid's
    process start-time and re-verify it just before each signal; if it changed,
    the pid was reused and we refuse to signal it. When the platform can't
    report a start-time (``proc_start_time`` → None), we degrade to the old
    behaviour rather than block the stop.
    """
    from . import _ipc
    from . import platforms
    import signal, time

    pid = _ipc.ping_sync(cfg.name, timeout=1.0)
    if pid is None:
        # No live daemon. Still clean up stale files so the next `serve` can
        # bind freshly without manual intervention.
        _ipc.cleanup_endpoint(cfg.name)
        print(f"no live daemon at name={cfg.name!r}; cleaned up stale files",
              file=sys.stderr)
        return 0

    start0 = platforms.proc_start_time(pid)

    def _same_process() -> bool:
        """True if pid still names the daemon we pinged. Unknowable (None
        start-time) counts as True so the guard never blocks a legit stop."""
        if start0 is None:
            return True
        return platforms.proc_start_time(pid) == start0

    if not _same_process():
        _ipc.cleanup_endpoint(cfg.name)
        print(f"daemon pid {pid} was recycled by another process; not "
              f"signalling it", file=sys.stderr)
        return 0
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        _ipc.cleanup_endpoint(cfg.name)
        return 0
    # Wait for the daemon to exit gracefully.
    deadline = time.monotonic() + args.timeout
    while time.monotonic() < deadline:
        if _ipc.ping_sync(cfg.name, timeout=0.3) is None:
            return 0
        time.sleep(0.1)
    # Still alive — force, but only if the pid is still the same process.
    if _same_process():
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    else:
        print(f"daemon pid {pid} was recycled before SIGKILL; not signalling "
              f"it", file=sys.stderr)
    _ipc.cleanup_endpoint(cfg.name)
    return 0


def _cmd_backend_info(args, cfg: Config) -> int:
    """Probe the running daemon for its backend identity. Same shape as
    `BrowserDaemon.getBackendInfo`'s ws response so the mode_b_client
    subprocess shim can parse it directly."""
    import asyncio
    _validate_daemon_name(cfg.name)
    return asyncio.run(_run_backend_info(args, cfg))


async def _run_backend_info(args, cfg: Config) -> int:
    from . import _ipc
    pid = await _ipc.ping_async(cfg.name, timeout=1.0)
    if pid is None:
        if args.json:
            print(json.dumps({"running": False, "name": cfg.name},
                             sort_keys=True))
        else:
            print(f"daemon[{cfg.name}] not running", file=sys.stderr)
        return 2
    try:
        info = await _rpc_via_ws(
            cfg, "BrowserDaemon.getBackendInfo", {},
            client_label="cli-backend-info", timeout=5.0,
        )
    except (Unavailable, DaemonError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 3
    # Surface as `backend` (alias of `name`) for callers like
    # ModeBClient.assert_backend_matches that read either key.
    payload = {
        "running": True,
        "name": info.get("name"),
        "backend": info.get("name"),
        "kind": info.get("kind"),
        "schema_version": info.get("schema_version", 1),
    }
    print(json.dumps(payload, sort_keys=True))
    return 0


def _cmd_stats(args, cfg: Config) -> int:
    """v0.5: query the running daemon's in-process metrics via the
    `BrowserDaemon.stats` CDP-namespace method, print to stdout.

    Connects to the daemon's unix socket as a normal client. Exits with
    code 2 if the daemon isn't running (matching `status`).
    """
    import asyncio
    return asyncio.run(_run_stats(args, cfg))


async def _run_stats(args, cfg: Config) -> int:
    from . import _ipc
    # We're inside `asyncio.run()` already (via _cmd_stats). Nested
    # asyncio.run raises RuntimeError, so use the async ping directly —
    # ping_sync's fallback returns None and we'd misreport "not running"
    # even when a daemon was listening.
    pid = await _ipc.ping_async(cfg.name, timeout=1.0)
    if pid is None:
        print(f"daemon[{cfg.name}] not running", file=sys.stderr)
        return 2

    import websockets
    sock_path = _ipc.sock_path(cfg.name)
    if _ipc.IS_WINDOWS:
        ep = _ipc.endpoint_describe(cfg.name)
        ws_url = f"ws://127.0.0.1:{ep['port']}/?client=stats-cli&token={ep['token']}"
        conn = await websockets.connect(ws_url, compression=None)
    else:
        conn = await websockets.unix_connect(
            str(sock_path),
            uri="ws://localhost/?client=stats-cli",
            compression=None,
        )
    try:
        await conn.send(json.dumps({"id": 1, "method": "BrowserDaemon.stats"}))
        # Drain until we see id=1.
        for _ in range(20):
            raw = await asyncio.wait_for(conn.recv(), timeout=3.0)
            msg = json.loads(raw)
            if msg.get("id") == 1 and "result" in msg:
                snap = msg["result"]
                if args.json:
                    print(json.dumps(snap, sort_keys=True))
                else:
                    # Tab-separated key=value, one per line.
                    for k in sorted(snap.keys()):
                        print(f"{k}\t{snap[k]}")
                return 0
        print("daemon did not respond to BrowserDaemon.stats", file=sys.stderr)
        return 3
    finally:
        try:
            await conn.close()
        except Exception:
            pass


def _cmd_status(args, cfg: Config) -> int:
    """Report endpoint + liveness. JSON shape used by Skill for status pings."""
    from . import _ipc
    pid, version = _ipc.ping_status_sync(cfg.name, timeout=1.0)
    ep = _ipc.endpoint_describe(cfg.name)
    status = {
        "schema_version": 1,
        "name": cfg.name,
        "alive": pid is not None,
        "pid": pid,
        "version": version,
        "endpoint": ep,
    }
    if args.json:
        print(json.dumps(status, sort_keys=True))
    else:
        if pid is None:
            print(f"daemon[{cfg.name}] not running")
        else:
            print(f"daemon[{cfg.name}] alive (pid {pid})")
            if ep["transport"] == "unix":
                print(f"  socket: {ep['path']}")
            else:
                print(f"  tcp:    127.0.0.1:{ep['port']}  token={ep['token']}")
    return 0 if pid is not None else 2


def _cmd_disconnect(args, cfg: Config) -> int:
    """Open a transient ws to the daemon, fire BrowserDaemon.disconnect, exit.

    Equivalent to the RPC over an established connection — Skill can use either.
    """
    from . import _ipc
    return _run(_disconnect_via_ws(cfg, args.reason))


async def _disconnect_via_ws(cfg: Config, reason: str) -> int:
    """Lightweight ws client that says BrowserDaemon.disconnect and reads the
    ack. We bypass cdp-use intentionally — we don't need framing, just one
    request + one response."""
    import websockets
    from . import _ipc

    if _ipc.IS_WINDOWS:
        port, token = _ipc.read_port_file(cfg.name)
        if port is None:
            print("no daemon running", file=sys.stderr)
            return 2
        url = f"ws://127.0.0.1:{port}/?token={token}&client=cli-disconnect"
        try:
            async with websockets.connect(url, compression=None) as ws:
                await ws.send(json.dumps({
                    "id": 1, "method": "BrowserDaemon.disconnect",
                    "params": {"reason": reason},
                }))
                await asyncio.wait_for(ws.recv(), timeout=2.0)
        except Exception as e:
            print(f"disconnect failed: {e}", file=sys.stderr)
            return 2
    else:
        path = _ipc.sock_path(cfg.name)
        if not path.exists():
            print("no daemon running", file=sys.stderr)
            return 2
        # `ws+unix:` URL scheme + path: websockets accepts unix= kwarg.
        try:
            async with websockets.unix_connect(str(path), uri="ws://localhost/?client=cli-disconnect",
                                               compression=None) as ws:
                await ws.send(json.dumps({
                    "id": 1, "method": "BrowserDaemon.disconnect",
                    "params": {"reason": reason},
                }))
                await asyncio.wait_for(ws.recv(), timeout=2.0)
        except Exception as e:
            print(f"disconnect failed: {e}", file=sys.stderr)
            return 2
    return 0


def _cmd_attach_active(args, cfg: Config) -> int:
    """Ask the running daemon (extension backend) to attach the
    currently-focused-window active tab. Prints the result as JSON or as
    `targetId<TAB>url<TAB>title`. Exits 1 if the daemon errored.
    """
    return _run(_attach_active_via_ws(cfg, args))


async def _attach_active_via_ws(cfg: Config, args) -> int:
    import websockets
    from . import _ipc

    if _ipc.IS_WINDOWS:
        port, token = _ipc.read_port_file(cfg.name)
        if port is None:
            print("no daemon running", file=sys.stderr)
            return 2
        url = f"ws://127.0.0.1:{port}/?token={token}&client=cli-attach-active"
        try:
            async with websockets.connect(url, compression=None) as ws:
                return await _attach_active_roundtrip(ws, args)
        except Exception as e:
            print(f"attach-active failed: {e}", file=sys.stderr)
            return 1
    path = _ipc.sock_path(cfg.name)
    if not path.exists():
        print("no daemon running", file=sys.stderr)
        return 2
    try:
        async with websockets.unix_connect(
                str(path), uri="ws://localhost/?client=cli-attach-active",
                compression=None) as ws:
            return await _attach_active_roundtrip(ws, args)
    except Exception as e:
        print(f"attach-active failed: {e}", file=sys.stderr)
        return 1


async def _attach_active_roundtrip(ws, args) -> int:
    await ws.send(json.dumps({
        "id": 1, "method": "BrowserDaemon.attachActiveTab",
    }))
    # Drain until we see id=1 — lifecycle events (upstreamConnecting,
    # upstreamReady) can arrive ahead of the response.
    for _ in range(20):
        raw = await asyncio.wait_for(ws.recv(), timeout=15.0)
        msg = json.loads(raw)
        if msg.get("id") != 1:
            continue
        if "error" in msg:
            err = msg["error"] or {}
            print(f"attach-active error: {err.get('message', str(err))}",
                  file=sys.stderr)
            return 1
        result = msg.get("result") or {}
        if args.json:
            print(json.dumps(result, sort_keys=True))
        else:
            print(f"{result.get('targetId')}\t{result.get('url', '')}\t"
                  f"{result.get('title', '')}")
        return 0
    print("daemon did not respond to BrowserDaemon.attachActiveTab",
          file=sys.stderr)
    return 1


def _cmd_logs(args, cfg: Config) -> int:
    """Print log file path, or tail -f it."""
    from . import _ipc
    log = _ipc.log_path(cfg.name)
    if not args.follow:
        print(log)
        return 0
    if not log.exists():
        print(f"no log file at {log}", file=sys.stderr)
        return 2
    # tail -f — best to just exec tail rather than reimplement.
    os.execvp("tail", ["tail", "-n", "+0", "-f", str(log)])
    return 0  # unreachable


def _cmd_doctor(args, cfg: Config) -> int:
    from .doctor import doctor

    out = _run(doctor(cfg, backend=getattr(args, "backend", None),
                      probe_ws=getattr(args, "probe_ws", False)))
    if args.json:
        print(json.dumps(out, sort_keys=True))
    else:
        _pretty_doctor(out)
    return 0


def _cmd_list_backends(args, cfg: Config) -> int:
    from .doctor import list_backends

    out = _run(list_backends(cfg))
    if args.json:
        print(json.dumps(out, sort_keys=True))
    else:
        for entry in out["backends"]:
            print(f"{entry['name']:<12} kind={entry['kind']:<12} "
                  f"recommended_mode={entry['recommended_mode']} "
                  f"ux_cost={entry['ux_cost']}")
    return 0


def _cmd_active_tab(args, cfg: Config) -> int:
    from .active_tab import active_tab

    info = _run(active_tab(cfg))
    if info is None:
        if args.json:
            print(json.dumps({
                "schema_version": 1,
                "targetId": None,
                "accuracy": "unknown",
                "since_seconds": None,
            }, sort_keys=True))
        else:
            print("")  # spec §5.4: empty line + exit 2 when no active tab
        return 2
    if args.json:
        print(json.dumps({"schema_version": 1, **info}, sort_keys=True))
    else:
        # tab-separated: targetId\turl\ttitle\taccuracy
        print(f"{info['targetId']}\t{info['url']}\t{info['title']}\t{info['accuracy']}")
    return 0


def _cmd_launch_chrome(args, cfg: Config) -> int:
    from .launch_chrome import launch_chrome

    out = _run(launch_chrome(
        cfg,
        profile=args.profile,
        persistent=not args.tmp,  # --tmp wins over --persistent default
        chrome_binary=args.chrome_binary,
        port=args.port,
        timeout=args.timeout,
        allow_default_profile=args.allow_default_profile,
    ))
    if args.json:
        print(json.dumps(out, sort_keys=True))
    else:
        # Bare URL, matching `url` discipline.
        print(out["ws_url"])
    return 0


def _cmd_version(args, cfg: Config) -> int:
    print(f"browser-daemon {__version__}")
    return 0


async def _rpc_via_ws(cfg: Config, method: str, params: dict,
                      *, client_label: str, timeout: float = 10.0) -> dict:
    """Open a transient ws to the running daemon, send one BrowserDaemon.*
    RPC, read the response, close. Mirrors `_disconnect_via_ws` but returns
    the parsed result (or raises with the daemon's error message).

    Used by `open-background` and `close-tab` subcommands.
    """
    import websockets
    from . import _ipc

    async def _drain_until_response(ws) -> dict:
        # Lifecycle events (upstreamConnecting/Ready) can arrive ahead of the
        # actual id=1 response, especially when lazy-open is triggered by the
        # RPC. Drain frames until we see ours. Mirrors `_attach_active_roundtrip`.
        for _ in range(20):
            raw = await asyncio.wait_for(ws.recv(), timeout=timeout)
            msg = json.loads(raw)
            if msg.get("id") == 1:
                return msg
        raise DaemonError(f"{method} no id=1 response after 20 frames")

    if _ipc.IS_WINDOWS:
        port, token = _ipc.read_port_file(cfg.name)
        if port is None:
            raise Unavailable("no daemon running")
        url = f"ws://127.0.0.1:{port}/?token={token}&client={client_label}"
        async with websockets.connect(url, compression=None) as ws:
            await ws.send(json.dumps({
                "id": 1, "method": method, "params": params,
            }))
            msg = await _drain_until_response(ws)
    else:
        path = _ipc.sock_path(cfg.name)
        if not path.exists():
            raise Unavailable("no daemon running")
        async with websockets.unix_connect(
            str(path),
            uri=f"ws://localhost/?client={client_label}",
            compression=None,
        ) as ws:
            await ws.send(json.dumps({
                "id": 1, "method": method, "params": params,
            }))
            msg = await _drain_until_response(ws)
    if "error" in msg:
        err = msg["error"] or {}
        raise DaemonError(
            f"{method} failed: {err.get('message', err)} (code={err.get('code')})"
        )
    result = msg.get("result")
    if not isinstance(result, dict):
        raise DaemonError(f"{method} returned non-dict result: {result!r}")
    return result


async def _userscript_call_ws(cfg: Config, method: str, params: dict,
                                timeout: float = 5.0) -> dict:
    return await _rpc_via_ws(
        cfg, method, params, client_label="cli-userscript", timeout=timeout)


def _cmd_userscript(args, cfg: Config | None = None) -> int:
    if cfg is None:
        cfg = load()
    if isinstance(args, list):
        parser = _build_parser()
        ns = parser.parse_args(["userscript", *args])
    else:
        ns = args
    action = getattr(ns, "userscript_cmd", None)
    if not action:
        print("usage: browser-daemon userscript {push,list,remove,toggle,logs} ...",
              file=sys.stderr)
        return 1

    try:
        if action in {"push", "install"}:
            from .userscripts import parse_userscript
            if ns.file == "-":
                text = sys.stdin.read()
            else:
                with open(ns.file, encoding="utf-8") as f:
                    text = f.read()
            us = parse_userscript(text)
            result = _run(_userscript_call_ws(
                cfg, "BrowserDaemon.userscript.install", {"script": us.to_payload()}))
            sync = result.get("sync", {}) or {}
            print(json.dumps({
                "id": result.get("id", us.id),
                "identity": result.get("identity", us.identity),
                "warnings": us.warnings,
                "sync": sync,
            }, sort_keys=True))
            # Surface header warnings and (crucially) a failed sync to stderr so
            # a stored-but-not-injected script doesn't read as plain success.
            for w in us.warnings:
                print(f"warning: {w}", file=sys.stderr)
            if sync.get("ok") is False:
                reason = sync.get("reason")
                if reason:
                    print(f"warning: stored but NOT active: {reason}", file=sys.stderr)
                    if "userScripts API unavailable" in str(reason):
                        print("hint: enable the extension's 'Allow user scripts' "
                              "toggle at chrome://extensions (Chrome 138+), or "
                              "developer mode on older Chrome.", file=sys.stderr)
                for f in sync.get("failed") or []:
                    print(f"warning: registration failed for "
                          f"{f.get('identity') or f.get('id')}: {f.get('error')}",
                          file=sys.stderr)
                return 2
            return 0

        if action == "list":
            params = {"site": ns.site} if ns.site else {}
            result = _run(_userscript_call_ws(
                cfg, "BrowserDaemon.userscript.list", params))
        elif action == "remove":
            result = _run(_userscript_call_ws(
                cfg, "BrowserDaemon.userscript.remove", {"key": ns.key}))
        elif action == "toggle":
            enabled = str(ns.enabled).lower() in {"1", "true", "yes", "on"}
            result = _run(_userscript_call_ws(
                cfg, "BrowserDaemon.userscript.toggle",
                {"key": ns.key, "enabled": enabled}))
        elif action == "logs":
            # NB: the relay envelope reserves the "id" key for the RPC
            # request id (relay._request overwrites it), so the script-id
            # filter must travel under a non-colliding key.
            params = {"limit": ns.limit}
            if ns.id:
                params["scriptId"] = ns.id
            result = _run(_userscript_call_ws(
                cfg, "BrowserDaemon.userscript.logs", params))
        else:
            print(f"unknown userscript action: {action}", file=sys.stderr)
            return 1
    except UserError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    except Unavailable as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    except DaemonError as e:
        print(f"error: {e}", file=sys.stderr)
        return 3

    print(json.dumps(result, sort_keys=True))
    return 0


def _cmd_open_background(args, cfg: Config) -> int:
    try:
        result = _run(_rpc_via_ws(
            cfg,
            "BrowserDaemon.openBackgroundTab",
            {"url": args.url, "groupName": args.group},
            client_label="cli-open-background",
            timeout=15.0,
        ))
    except Unavailable as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    except DaemonError as e:
        print(f"error: {e}", file=sys.stderr)
        return 3
    # Always emit JSON — single-line spec §5.1 discipline; the response is
    # structured so a tab-separated form would lose fields.
    print(json.dumps(result, sort_keys=True))
    return 0


def _cmd_close_tab(args, cfg: Config) -> int:
    if not args.session_id and not args.target_id:
        print("error: provide --session-id or --target-id", file=sys.stderr)
        return 2
    params: dict = {}
    if args.session_id:
        params["sessionId"] = args.session_id
    if args.target_id:
        params["targetId"] = args.target_id
    try:
        result = _run(_rpc_via_ws(
            cfg,
            "BrowserDaemon.closeTab",
            params,
            client_label="cli-close-tab",
            timeout=10.0,
        ))
    except Unavailable as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    except DaemonError as e:
        print(f"error: {e}", file=sys.stderr)
        return 3
    print(json.dumps(result, sort_keys=True))
    return 0


def _cmd_end_session(args, cfg: Config) -> int:
    """P5: tear down a browser-skill session's extension tabs (owned closed,
    borrowed kept). Prints the {closed, kept} JSON result."""
    es_params: dict = {"session": args.session}
    if getattr(args, "group_name", None):
        es_params["groupName"] = args.group_name
    try:
        result = _run(_rpc_via_ws(
            cfg,
            "BrowserDaemon.endSession",
            es_params,
            client_label="cli-end-session",
            timeout=10.0,
        ))
    except Unavailable as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    except DaemonError as e:
        print(f"error: {e}", file=sys.stderr)
        return 3
    print(json.dumps(result, sort_keys=True))
    return 0


# ---- LaunchAgent service (macOS) ----------------------------------------
#
# Goal: the daemon is a long-running service, not a per-session subprocess.
# macOS LaunchAgents are the right primitive — Linux/systemd-user support
# is deferred (no users hitting it yet on this codebase).

_LAUNCHAGENT_LABEL_PREFIX = "com.browser-daemon."

# Per the IPC contract (see `_ipc._NAME_RE`) but a touch wider — we accept
# dots too because LaunchAgent labels routinely contain them. The cap stays
# 64 chars to keep filesystem paths bounded.
_DAEMON_NAME_RE = re.compile(r"\A[A-Za-z0-9._-]{1,64}\Z")


def _validate_daemon_name(name: str) -> str:
    """Reject anything outside ``[A-Za-z0-9._-]{1,64}`` BEFORE it touches the
    filesystem (plist path), the LaunchAgent label, or any XML interpolation.

    Without this guard ``--name '../../tmp/evil'`` would write the plist
    outside ``~/Library/LaunchAgents`` and ``--name 'weird<chars>'`` would
    yield malformed plist XML. We also rely on this elsewhere to keep
    cli-level handling consistent with `_ipc.check_name`'s socket-naming
    constraint.
    """
    if not _DAEMON_NAME_RE.match(name or ""):
        raise UserError(
            "--name must be alphanumeric/dot/dash/underscore, 1-64 chars; "
            f"got {name!r}"
        )
    return name


def _launchagent_dir() -> "Path":
    from pathlib import Path
    return Path.home() / "Library" / "LaunchAgents"


def _launchagent_plist_path(name: str) -> "Path":
    return _launchagent_dir() / f"{_LAUNCHAGENT_LABEL_PREFIX}{name}.plist"


def _resolve_browser_daemon_bin() -> str:
    """Find the absolute path to the `browser-daemon` console script. The
    plist needs a fully-qualified path; LaunchAgents don't inherit the
    user's shell PATH."""
    import shutil
    path = shutil.which("browser-daemon")
    if path:
        return path
    # Fallback to "<sys.prefix>/bin/browser-daemon".
    from pathlib import Path
    candidate = Path(sys.prefix) / "bin" / "browser-daemon"
    if candidate.exists():
        return str(candidate)
    raise UserError(
        "browser-daemon binary not found on PATH; "
        "install it via pip/uv before running `browser-daemon install`"
    )


def _build_plist(*, label: str, name: str, backend: str,
                 extension_port: int | None) -> str:
    """Emit the plist content. Kept inline (no XML lib) — the schema is
    fixed + tiny, and we avoid a dependency. Every interpolated value passes
    through ``xml.sax.saxutils.escape`` (belt + suspenders alongside the
    ``_validate_daemon_name`` guard the callers must run first). Pure: no
    side effects — the caller is responsible for creating the log dir.
    """
    bin_path = _resolve_browser_daemon_bin()
    args = [bin_path, "serve", "--backend", backend, "--name", name]
    if extension_port is not None:
        args += ["--extension-port", str(extension_port)]
    log_dir = os.path.expanduser("~/.cache/browser-daemon/logs")
    stdout_path = f"{log_dir}/{name}.stdout.log"
    stderr_path = f"{log_dir}/{name}.stderr.log"
    # PATH carried over so any subprocess the daemon spawns (e.g. chrome)
    # is discoverable. /usr/local/bin + /opt/homebrew/bin cover both
    # Intel and Apple Silicon Homebrew layouts. Constant, no escape needed.
    env_path = "/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin"
    arg_xml = "\n        ".join(
        f"<string>{_xml_escape(a)}</string>" for a in args
    )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" '
        '"http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
        '<plist version="1.0">\n'
        '<dict>\n'
        f'    <key>Label</key><string>{_xml_escape(label)}</string>\n'
        '    <key>ProgramArguments</key>\n'
        '    <array>\n'
        f'        {arg_xml}\n'
        '    </array>\n'
        '    <key>RunAtLoad</key><true/>\n'
        '    <key>KeepAlive</key>\n'
        '    <dict>\n'
        '        <key>SuccessfulExit</key><false/>\n'
        '        <key>Crashed</key><true/>\n'
        '    </dict>\n'
        '    <key>EnvironmentVariables</key>\n'
        '    <dict>\n'
        f'        <key>PATH</key><string>{env_path}</string>\n'
        '    </dict>\n'
        f'    <key>StandardOutPath</key><string>{_xml_escape(stdout_path)}</string>\n'
        f'    <key>StandardErrorPath</key><string>{_xml_escape(stderr_path)}</string>\n'
        f'    <key>WorkingDirectory</key><string>{_xml_escape(os.path.expanduser("~"))}</string>\n'
        '</dict>\n'
        '</plist>\n'
    )


def _launchctl(*args: str) -> tuple[int, str, str]:
    import subprocess
    try:
        proc = subprocess.run(["launchctl", *args],
                              capture_output=True, text=True, timeout=10)
        return proc.returncode, proc.stdout, proc.stderr
    except (FileNotFoundError, subprocess.TimeoutExpired) as e:
        return -1, "", str(e)


def _cmd_install(args, cfg: Config) -> int:
    if sys.platform != "darwin":
        print("error: `install` is macOS-only (LaunchAgent); "
              "for Linux run `browser-daemon serve` from a systemd-user "
              "unit yourself for now", file=sys.stderr)
        return 1
    name = args.name or os.environ.get("BD_NAME") or "default"
    _validate_daemon_name(name)
    label = f"{_LAUNCHAGENT_LABEL_PREFIX}{name}"
    plist_path = _launchagent_plist_path(name)
    if plist_path.exists() and not args.force:
        print(f"error: {plist_path} already exists. "
              f"Use --force to replace.", file=sys.stderr)
        return 1
    plist_path.parent.mkdir(parents=True, exist_ok=True)
    # Create the log dir before launchctl tries to write to it. Kept out of
    # `_build_plist` so the generator stays pure / unit-testable (N-1).
    log_dir = os.path.expanduser("~/.cache/browser-daemon/logs")
    os.makedirs(log_dir, exist_ok=True)
    content = _build_plist(
        label=label, name=name, backend=args.backend,
        extension_port=args.extension_port,
    )
    # If --force and the plist exists, unload the old one first so launchctl
    # picks up the new ProgramArguments cleanly.
    if plist_path.exists():
        _launchctl("unload", str(plist_path))
    plist_path.write_text(content)
    rc, _, err = _launchctl("load", "-w", str(plist_path))
    if rc != 0:
        # Rollback: remove the just-written plist so a re-run isn't blocked
        # by the "already exists" check (L-3).
        plist_path.unlink(missing_ok=True)
        print(f"error: launchctl load failed: {err.strip()}",
              file=sys.stderr)
        return 3
    print(json.dumps({
        "ok": True,
        "name": name,
        "label": label,
        "plist": str(plist_path),
        "backend": args.backend,
        "extension_port": args.extension_port,
    }, sort_keys=True))
    return 0


def _cmd_uninstall(args, cfg: Config) -> int:
    if sys.platform != "darwin":
        print("error: `uninstall` is macOS-only", file=sys.stderr)
        return 1
    name = args.name or os.environ.get("BD_NAME") or "default"
    _validate_daemon_name(name)
    plist_path = _launchagent_plist_path(name)
    if not plist_path.exists():
        print(json.dumps({"ok": False, "name": name,
                          "reason": "no LaunchAgent installed"},
                         sort_keys=True))
        return 0
    rc, _, err = _launchctl("unload", str(plist_path))
    # Even on unload failure (e.g. wasn't loaded), we still remove the plist.
    plist_path.unlink()
    payload = {"ok": True, "name": name, "removed": str(plist_path)}
    if rc != 0 and err.strip():
        payload["unload_warning"] = err.strip()
    print(json.dumps(payload, sort_keys=True))
    return 0


def _cmd_list(args, cfg: Config) -> int:
    from pathlib import Path
    instances = []
    # Installed LaunchAgents (macOS).
    la_dir = _launchagent_dir()
    if la_dir.exists():
        for plist in sorted(la_dir.glob(f"{_LAUNCHAGENT_LABEL_PREFIX}*.plist")):
            name = plist.stem[len(_LAUNCHAGENT_LABEL_PREFIX):]
            running_pid = _ipc_ping(name)
            instances.append({
                "name": name,
                "service": "launchagent",
                "plist": str(plist),
                "running": running_pid is not None,
                "pid": running_pid,
            })
    # Also surface daemons reachable on a socket but NOT registered as
    # LaunchAgents (manual `browser-daemon serve` invocations).
    from . import _ipc
    sock_dir = _ipc.sock_path("default").parent
    seen_names = {i["name"] for i in instances}
    if sock_dir.exists():
        for sock in sorted(sock_dir.glob("browser-daemon-*.sock")):
            stem = sock.stem  # e.g. "browser-daemon-myrepl"
            name = stem[len("browser-daemon-"):]
            if name in seen_names:
                continue
            running_pid = _ipc_ping(name)
            if running_pid is None:
                continue
            instances.append({
                "name": name,
                "service": "manual",
                "plist": None,
                "running": True,
                "pid": running_pid,
            })
    if args.json:
        print(json.dumps({"instances": instances}, sort_keys=True))
        return 0
    if not instances:
        print("no daemon instances found")
        return 0
    print(f"{'NAME':<20} {'SERVICE':<12} {'RUNNING':<8} {'PID':<8}")
    for inst in instances:
        running = "yes" if inst["running"] else "no"
        pid = str(inst["pid"]) if inst["pid"] else "-"
        print(f"{inst['name']:<20} {inst['service']:<12} {running:<8} {pid:<8}")
    return 0


def _ipc_ping(name: str) -> int | None:
    """Tiny wrapper around ipc.ping_sync that swallows everything."""
    try:
        from . import _ipc
        return _ipc.ping_sync(name, timeout=0.5)
    except Exception:
        return None


_DISPATCH = {
    "url": _cmd_url,
    "doctor": _cmd_doctor,
    "list-backends": _cmd_list_backends,
    "active-tab": _cmd_active_tab,
    "launch-chrome": _cmd_launch_chrome,
    "version": _cmd_version,
    # v0.2
    "serve": _cmd_serve,
    "stop": _cmd_stop,
    "status": _cmd_status,
    "disconnect": _cmd_disconnect,
    "logs": _cmd_logs,
    # v0.5
    "stats": _cmd_stats,
    "backend-info": _cmd_backend_info,
    # v0.5.4 — extension backend
    "attach-active": _cmd_attach_active,
    "open-background": _cmd_open_background,
    "close-tab": _cmd_close_tab,
    "end-session": _cmd_end_session,
    "userscript": _cmd_userscript,
    # v0.5.5 — LaunchAgent service (macOS) so the daemon is long-running.
    "install": _cmd_install,
    "uninstall": _cmd_uninstall,
    "list": _cmd_list,
}


# ---- pretty print ----------------------------------------------------------


def _pretty_doctor(out: dict) -> None:
    rec = out.get("recommended")
    print(f"recommended: {rec or '(none available)'}")
    print()
    for entry in out["backends"]:
        mark = "OK " if entry["available"] else "-- "
        print(f"  {mark} {entry['name']:<12} ux_cost={entry['ux_cost']}")
        if entry["detail"]:
            print(f"      detail: {entry['detail']}")
        if entry["ux_warning"]:
            print(f"      warning: {entry['ux_warning']}")
        if entry["needs_user_action"]:
            print(f"      next: {entry['needs_user_action']}")


if __name__ == "__main__":
    _entry()
