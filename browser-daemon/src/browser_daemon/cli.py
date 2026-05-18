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
import sys
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
    p_url.add_argument("--quiet", action="store_true",
                       help="suppress the autoconnect popup-hazard stderr warning")
    _add_name(p_url)

    # serve (v0.2)
    p_serve = sub.add_parser("serve", help="run the long-lived Mode B daemon (v0.2)")
    _add_common(p_serve)
    _add_port(p_serve)
    _add_name(p_serve)
    # v0.5.3 Task #24: extension relay port override. Useful when default
    # 19988 is occupied (e.g., playwriter coexisting on the dev machine).
    # Precedence: this flag > BD_EXTENSION_PORT > toml > 19988 default.
    p_serve.add_argument(
        "--extension-port", type=int, default=None, metavar="N",
        help=("Bind the extension relay ws server on this port instead of "
              "the default 19988. Only relevant when --backend extension. "
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

    return p


def _add_common(sp: argparse.ArgumentParser) -> None:
    sp.add_argument("--backend", choices=names(),
                    help="pin to one backend; otherwise the chain runs env -> rdp -> autoconnect")
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
    # P0 defense: warn loudly when the explicit `autoconnect` backend is
    # selected for a Mode A short-conn — every call there triggers a Chrome
    # popup and Chrome 144+ may freeze on accumulation. Skip when --quiet,
    # --mode-b-proxy (Mode B doesn't actually open ws here), or when the
    # backend wasn't explicitly chosen (auto chain — autoconnect comes last).
    if (cfg.backend == "autoconnect"
            and not getattr(args, "quiet", False)
            and not getattr(args, "mode_b_proxy", False)):
        print(
            "WARNING: autoconnect path triggers Chrome's 'Allow remote "
            "debugging' popup per ws handshake; Chrome 144+ may freeze on "
            "accumulation. Consider `browser-daemon serve --backend "
            "autoconnect` to reuse a single popup, or "
            "`browser-daemon launch-chrome --port <N> --profile <P>` for "
            "an isolated Chrome with zero popups. "
            "Pass --quiet to suppress this warning.",
            file=sys.stderr,
        )

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
    """Run the long-lived Mode B daemon (§5 v0.2)."""
    from .server.listener import run_serve
    return _run(run_serve(cfg))


def _cmd_stop(args, cfg: Config) -> int:
    """Send SIGTERM to a running daemon, wait briefly, fall back to SIGKILL.

    We do NOT trust the pid file alone — we ping first to verify it's our
    daemon, then signal that pid. (Mirrors browser-harness `_ipc.identify`.)
    """
    from . import _ipc
    import signal, time

    pid = _ipc.ping_sync(cfg.name, timeout=1.0)
    if pid is None:
        # No live daemon. Still clean up stale files so the next `serve` can
        # bind freshly without manual intervention.
        _ipc.cleanup_endpoint(cfg.name)
        print(f"no live daemon at name={cfg.name!r}; cleaned up stale files",
              file=sys.stderr)
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
    # Still alive — force.
    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    _ipc.cleanup_endpoint(cfg.name)
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
    pid = _ipc.ping_sync(cfg.name, timeout=1.0)
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
    pid = _ipc.ping_sync(cfg.name, timeout=1.0)
    ep = _ipc.endpoint_describe(cfg.name)
    status = {
        "schema_version": 1,
        "name": cfg.name,
        "alive": pid is not None,
        "pid": pid,
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
