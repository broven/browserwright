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
import time
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
        prog="browserwright-daemon",
        description=(
            "Long-lived local CDP proxy daemon: one global `serve` process "
            "that routes browserwright sessions to any local Chrome "
            "(extension relay, rdp, or an env-supplied CDP endpoint)."
        ),
    )
    sub = p.add_subparsers(dest="cmd", metavar="<subcommand>")

    # serve (v0.2)
    p_serve = sub.add_parser("serve", help="run the long-lived global daemon")
    _add_common(p_serve)
    _add_port(p_serve)
    # v0.5.3 Task #24: extension relay port override. Useful when default
    # 19989 is occupied (e.g., a stale daemon process). playwriter sits on
    # 19988, so the default no longer collides with it.
    # Precedence: this flag > BD_EXTENSION_PORT > toml > 19989 default.
    p_serve.add_argument(
        "--extension-port", type=int, default=None, metavar="N",
        help=("Bind the extension relay ws server on this port instead of "
              "the default 19989. Only relevant to the shared extension relay. "
              "Equivalent to BD_EXTENSION_PORT env or "
              "[backends.extension].port in config.toml."))
    # Playwright facade (Phase C: auto-enabled). Expose a Playwright-facing
    # CDP ws+HTTP endpoint a real `chromium.connect_over_cdp` can connect to.
    # ON by default (port 19990) — the skill layer's heredoc `page`/`context`
    # depend on it. Pass an explicit port to override, or `--facade-port 0` to
    # disable. Equivalent to BD_FACADE_PORT env or `facade_port` in config.toml.
    p_serve.add_argument(
        "--facade-port", type=int, default=None, metavar="N",
        help=("bind the Playwright-facing CDP facade on this port "
              "(default: ON at 19990; pass 0 to disable). Lets a real "
              "Playwright client `connect_over_cdp(ws://127.0.0.1:N/cdp)` and "
              "the skill heredoc `page`/`context` drive the resolved Chrome."))

    # stop (v0.2)
    p_stop = sub.add_parser("stop", help="stop the running daemon")
    p_stop.add_argument("--timeout", type=float, default=5.0,
                        help="seconds to wait for graceful shutdown before SIGKILL")

    p_restart = sub.add_parser(
        "restart",
        help="restart the installed LaunchAgent daemon after an upgrade")
    p_restart.add_argument("--timeout", type=float, default=5.0,
                           help="seconds to wait for graceful unload/load")

    # status (v0.2)
    p_status = sub.add_parser("status", help="report the daemon's IPC endpoint + liveness")
    p_status.add_argument("--json", action="store_true")

    # logs (v0.2)
    p_logs = sub.add_parser("logs", help="print the daemon log file path or tail it")
    p_logs.add_argument("--follow", "-f", action="store_true", help="tail -f the log")

    # doctor
    p_doc = sub.add_parser("doctor", help="probe all backends (zero ws side effects by default)")
    _add_common(p_doc)
    p_doc.add_argument("--probe-ws", action="store_true",
                       help="opt-in: actually open a ws on each available backend (NOT IMPLEMENTED in v0.1)")
    p_doc.add_argument("--json", action="store_true", help="emit JSON instead of pretty text")

    # backend-info — what backend is the running daemon serving?
    # Skill clients shell out to this to decide whether the current
    # daemon matches their expected backend (refused-mismatch guard) and to
    # branch primitives on backend-specific quirks (e.g. extension's "0
    # attached tabs is actionable, not empty Chrome"). Internal plumbing for
    # the skill layer, so it is hidden from --help.
    p_bi = sub.add_parser("backend-info")
    p_bi.add_argument("--session", default=os.environ.get("BD_SESSION"),
                      help="browserwright session id (defaults to BD_SESSION)")
    p_bi.add_argument("--json", action="store_true")

    # attach-active (v0.5.4 — extension backend only)
    p_aa = sub.add_parser(
        "attach-active",
        help="(extension backend) attach the focused-window active tab without a popup click")
    p_aa.add_argument("--session", default=os.environ.get("BD_SESSION"),
                      help="browserwright session id (defaults to BD_SESSION)")
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
    p_ver = sub.add_parser("version", help="print the installed version and exit")
    p_ver.add_argument("--json", action="store_true")
    p_ver.add_argument("action", nargs="?", choices=["check"])

    # extension
    p_ext = sub.add_parser("extension", help="manage connected Chrome extensions")
    ext_sub = p_ext.add_subparsers(dest="extension_cmd", metavar="<action>")
    p_ext_reload = ext_sub.add_parser(
        "reload",
        help="ask connected unpacked extensions to reload from disk",
    )
    p_ext_reload.add_argument("--json", action="store_true")

    # open-background (Phase B — extension backend only)
    p_ob = sub.add_parser(
        "open-background",
        help=("open a Chrome tab in the background (group=Agent by default), "
              "attach chrome.debugger, print {sessionId,targetId,tabId,url,title,groupId}"),
    )
    p_ob.add_argument("--url", required=True, help="URL to open in the background tab")
    p_ob.add_argument("--group", default="Agent",
                      help="Chrome tab-group title to place the new tab in (default: Agent)")
    p_ob.add_argument("--session", default=os.environ.get("BD_SESSION"),
                      help="browserwright session id (defaults to BD_SESSION)")
    # Output is always JSON (spec §5.1 single-line discipline); no --json flag.

    # close-tab (Phase B — extension backend only)
    p_ct = sub.add_parser(
        "close-tab",
        help="close a tab by sessionId (persistent ws) or targetId (CLI)",
    )
    p_ct.add_argument("--session", default=os.environ.get("BD_SESSION"),
                      help="browserwright session id (defaults to BD_SESSION)")
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
        help="tear down a browserwright session's tabs: close owned, keep borrowed",
    )
    p_es.add_argument("--session", required=True,
                      help="the browserwright session id whose tabs to clean up")
    p_es.add_argument("--group-id", default=None, type=int,
                      help="durable numeric tab-group id for session teardown: "
                           "when the daemon lost this session's in-memory "
                           "binding (reconnect / restart), close the tabs in "
                           "this group instead (names aren't unique, so the id "
                           "is the key)")
    # Output is always JSON (single-line discipline); no --json flag.

    # kill-executor (Phase B) — reap ONLY a session's resident executor, no
    # browser teardown. Used by `session end` to avoid leaking an attach
    # session's executor (the full end-session path is create-only).
    p_ke = sub.add_parser(
        "kill-executor",
        help="reap a session's persistent executor subprocess (no browser teardown)",
    )
    p_ke.add_argument("--session", required=True,
                      help="the browserwright session id whose executor to reap")

    # userscript — resident extension userscripts
    p_us = sub.add_parser("userscript", help="manage resident extension userscripts")
    p_us.add_argument("--session", default=os.environ.get("BD_SESSION"),
                      help="browserwright session id (defaults to BD_SESSION)")
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
        help=("register the single global daemon as a macOS LaunchAgent "
              "(auto-start + KeepAlive)"))
    p_inst.add_argument("--backend", choices=names(), default=None,
                        help=argparse.SUPPRESS)
    p_inst.add_argument("--extension-port", type=int, default=None, metavar="N",
                        help="override the relay ws port (default 19989)")
    p_inst.add_argument("--force", action="store_true",
                        help="replace an existing LaunchAgent with the same name")

    sub.add_parser(
        "uninstall",
        help="remove the LaunchAgent (stops auto-start)")

    p_ls = sub.add_parser(
        "list",
        help="enumerate installed LaunchAgents + running daemon instances")
    p_ls.add_argument("--json", action="store_true",
                      help="emit JSON instead of pretty text")

    return p


def _add_common(sp: argparse.ArgumentParser) -> None:
    sp.add_argument("--backend", choices=names(),
                    help=("pin backend for this command; for `serve`, this only "
                          "chooses the shared upstream while session backends "
                          "still route per session"))
    sp.add_argument("--timeout", type=float, default=None,
                    help="per-backend timeout in seconds (default 5)")
    sp.add_argument("--config", help="optional toml config path; otherwise reads BD_CONFIG")
    sp.add_argument("-v", "--verbose", action="store_true")


def _add_port(sp: argparse.ArgumentParser) -> None:
    sp.add_argument("--port", type=int, default=None,
                    help="rdp backend port (default 9222 / config-backends.rdp.port)")


# ---- shared config building ------------------------------------------------


def _cfg_from_args(args) -> Config:
    return load(
        cli_backend=getattr(args, "backend", None),
        cli_timeout=getattr(args, "timeout", None),
        cli_port=getattr(args, "port", None),
        cli_chrome_binary=getattr(args, "chrome_binary", None),
        cli_config_path=getattr(args, "config", None),
        # v0.5.3 Task #24: serve-only flag; argparse Namespace shape varies
        # per subcommand, so getattr-with-default keeps non-serve calls clean.
        cli_extension_port=getattr(args, "extension_port", None),
        cli_facade_port=getattr(args, "facade_port", None),
    )


def _run(coro):
    return asyncio.run(coro)


# ---- subcommand handlers ---------------------------------------------------


def _cmd_serve(args, cfg: Config) -> int:
    """Run the long-lived Mode B daemon (§5 v0.2).

    Phase 2 (docs/refactor-single-daemon.md): there is exactly ONE global
    daemon and it serves BOTH backends simultaneously, routing per session. So
    `serve` no longer requires an explicit backend — a missing backend defaults
    to `extension`, which becomes the daemon's shared (real-browser) upstream
    with the always-on relay. rdp sessions get their own per-session upstream
    on top, dispatched by the ledger's immutable per-session backend. The old
    "fail loud on missing backend (to avoid a silent rdp fallback)" guard is
    gone: there is no single-backend lifetime to protect anymore.
    """
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

    pid = _ipc.ping_sync(timeout=1.0)
    if pid is None:
        # No live daemon. Still clean up stale files so the next `serve` can
        # bind freshly without manual intervention.
        _ipc.cleanup_endpoint()
        print("no live daemon; cleaned up stale files", file=sys.stderr)
        return 0

    start0 = platforms.proc_start_time(pid)

    def _same_process() -> bool:
        """True if pid still names the daemon we pinged. Unknowable (None
        start-time) counts as True so the guard never blocks a legit stop."""
        if start0 is None:
            return True
        return platforms.proc_start_time(pid) == start0

    if not _same_process():
        _ipc.cleanup_endpoint()
        print(f"daemon pid {pid} was recycled by another process; not "
              f"signalling it", file=sys.stderr)
        return 0
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        _ipc.cleanup_endpoint()
        return 0
    # Wait for the daemon to exit gracefully.
    deadline = time.monotonic() + args.timeout
    while time.monotonic() < deadline:
        if _ipc.ping_sync(timeout=0.3) is None:
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
    _ipc.cleanup_endpoint()
    return 0


def _cmd_backend_info(args, cfg: Config) -> int:
    """Probe the running daemon for its backend identity. Same shape as
    `BrowserwrightDaemon.getBackendInfo`'s ws response so the mode_b_client
    subprocess shim can parse it directly."""
    import asyncio
    return asyncio.run(_run_backend_info(args, cfg))


async def _run_backend_info(args, cfg: Config) -> int:
    from . import _ipc
    pid = await _ipc.ping_async(timeout=1.0)
    if pid is None:
        if args.json:
            print(json.dumps({"running": False}, sort_keys=True))
        else:
            print("daemon not running", file=sys.stderr)
        return 2
    try:
        params = {"bsSession": args.session} if args.session else {}
        info = await _rpc_via_ws(
            cfg, "BrowserwrightDaemon.getBackendInfo", params,
            client_label="cli-backend-info", timeout=5.0,
            browser_session=args.session,
        )
    except (Unavailable, DaemonError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 3
    # Surface as `backend` (alias of `name`) for callers like
    # ModeBClient.get_backend_info that read either key.
    payload = {
        "running": True,
        "name": info.get("name"),
        "backend": info.get("name"),
        "kind": info.get("kind"),
        "schema_version": info.get("schema_version", 1),
    }
    print(json.dumps(payload, sort_keys=True))
    return 0


def _cmd_status(args, cfg: Config) -> int:
    """Report endpoint + liveness. JSON shape used by Skill for status pings."""
    from . import _ipc, _stale
    pid, version = _ipc.ping_status_sync(timeout=1.0)
    probe_state = "ok" if pid is not None else "not_running"
    # issue #15 (2.1): pid of the process holding the relay/facade ports when
    # the control socket is dead but its file lingers — a half-alive daemon.
    port_holder_pid = None
    if pid is None and _ipc.sock_path().exists():
        deadline = time.monotonic() + 0.6
        while time.monotonic() < deadline:
            time.sleep(0.15)
            pid, version = _ipc.ping_status_sync(timeout=0.3)
            if pid is not None:
                probe_state = "ok_after_retry"
                break
        else:
            probe_state = "transient_probe_failed"
            # Still unresponsive, but its socket file is present — a half-alive
            # daemon may be holding the relay/facade ports. Report the truth
            # instead of a bare "not_running" that loops the user through
            # restarts that crash on EADDRINUSE.
            ports = _stale.daemon_tcp_ports(cfg)
            if any(_stale.port_is_listening("127.0.0.1", p) for p in ports):
                probe_state = "port_held_by_unresponsive_process"
                port_holder_pid = _stale.confirmed_stale_holder(ports)
                if port_holder_pid is None:
                    # lsof unavailable / non-daemon holder — surface the
                    # pid-file pid as a best-effort hint (never a kill target).
                    fp = _ipc.read_pid()
                    if fp and _stale.pid_alive(fp):
                        port_holder_pid = fp
    ep = _ipc.endpoint_describe()
    facade_ws, facade_port = _ipc.read_facade_file()
    status = {
        "schema_version": 1,
        "alive": pid is not None,
        "probe_state": probe_state,
        "pid": pid,
        # issue #15 (2.1): pid of the process holding the relay/facade ports
        # when the daemon is unresponsive. Actionable: `browserwright-daemon
        # restart` reclaims it, or kill it manually.
        "port_holder_pid": port_holder_pid,
        "version": version,
        "endpoint": ep,
        # Playwright facade discovery (Phase C). None when the facade is
        # disabled or the daemon predates auto-enable. The skill layer reads
        # this to `connect_over_cdp` the heredoc `page`/`context`.
        "facade": (
            {"ws": facade_ws, "port": facade_port}
            if (pid is not None and facade_ws) else None
        ),
    }
    if args.json:
        print(json.dumps(status, sort_keys=True))
    else:
        if pid is None:
            if probe_state == "port_held_by_unresponsive_process":
                held = f" (pid {port_holder_pid})" if port_holder_pid else ""
                print("daemon unresponsive but holding its ports"
                      f"{held} — a half-alive daemon.")
                print("  fix: `browserwright-daemon restart` reclaims the "
                      "ports; or kill the pid above manually.")
            else:
                print("daemon not running")
        else:
            print(f"daemon alive (pid {pid})")
            print(f"  socket: {ep['path']}")
            if facade_ws:
                print(f"  facade: {facade_ws}")
    return 0 if pid is not None else 2


def _cmd_attach_active(args, cfg: Config) -> int:
    """Ask the running daemon (extension backend) to attach the
    currently-focused-window active tab. Prints the result as JSON or as
    `targetId<TAB>url<TAB>title`. Exits 1 if the daemon errored.
    """
    if not args.session:
        print("error: provide --session or set BD_SESSION", file=sys.stderr)
        return 2
    return _run(_attach_active_via_ws(cfg, args))


async def _attach_active_via_ws(cfg: Config, args) -> int:
    import websockets
    from . import _ipc
    from urllib.parse import quote

    session = getattr(args, "session", None)
    session_q = f"&session={quote(str(session), safe='')}" if session else ""

    path = _ipc.sock_path()
    if not path.exists():
        print("no daemon running", file=sys.stderr)
        return 2
    try:
        async with websockets.unix_connect(
                str(path), uri=f"ws://localhost/?client=cli-attach-active{session_q}",
                compression=None) as ws:
            return await _attach_active_roundtrip(ws, args)
    except Exception as e:
        print(f"attach-active failed: {e}", file=sys.stderr)
        return 1


async def _attach_active_roundtrip(ws, args) -> int:
    params = {}
    if getattr(args, "session", None):
        params["bsSession"] = args.session
    await ws.send(json.dumps({
        "id": 1, "method": "BrowserwrightDaemon.attachActiveTab",
        "params": params,
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
    print("daemon did not respond to BrowserwrightDaemon.attachActiveTab",
          file=sys.stderr)
    return 1


def _cmd_logs(args, cfg: Config) -> int:
    """Print log file path, or tail -f it."""
    from . import _ipc
    log = _ipc.log_path()
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
    from browserwright.version import version_info

    info = version_info()
    if getattr(args, "action", None) == "check":
        relay_status = _extension_relay_status(cfg)
        if relay_status:
            info["daemon_version"] = relay_status.get("daemon_version")
            info["running_extensions"] = relay_status.get("extension_details") or []
        if args.json:
            print(json.dumps(info, sort_keys=True))
        else:
            if info["ok"]:
                print(f"browserwright-daemon {__version__} (versions ok)")
            else:
                for issue in info["issues"]:
                    print(f"{issue['code']}: {issue['message']}", file=sys.stderr)
            for ext in info.get("running_extensions") or []:
                print(
                    "extension "
                    f"{ext.get('install_id') or '?'} "
                    f"version={ext.get('browserwright_version') or ext.get('version') or '?'} "
                    f"daemon={ext.get('daemon_version') or '?'} "
                    f"drift={ext.get('version_drift') or '?'}"
                )
        return 0 if info["ok"] else 1
    if getattr(args, "json", False):
        print(json.dumps(info, sort_keys=True))
        return 0
    print(f"browserwright-daemon {__version__}")
    return 0


def _extension_relay_status(cfg: Config) -> dict | None:
    import httpx

    host, port = cfg.backends.extension.resolved_host_port()
    try:
        with httpx.Client(timeout=1.0, trust_env=False, mounts={}) as client:
            resp = client.get(f"http://{host}:{port}/__status__")
        if resp.status_code != 200:
            return None
        payload = resp.json()
        return payload if isinstance(payload, dict) else None
    except Exception:
        return None


def _cmd_extension(args, cfg: Config) -> int:
    action = getattr(args, "extension_cmd", None)
    if action == "reload":
        try:
            result = _run(_rpc_via_ws(
                cfg,
                "BrowserwrightDaemon.extension.reload",
                {"reason": "manual"},
                client_label="cli-extension-reload",
                timeout=8.0,
            ))
        except Unavailable as e:
            print(f"error: {e}", file=sys.stderr)
            return 2
        except DaemonError as e:
            print(f"error: {e}", file=sys.stderr)
            return 3
        if args.json:
            print(json.dumps(result, sort_keys=True))
        else:
            sent = int(result.get("sent", 0) or 0)
            print(f"reload requested for {sent} extension(s)")
        return 0
    print("usage: browserwright-daemon extension reload", file=sys.stderr)
    return 1


async def _rpc_via_ws(cfg: Config, method: str, params: dict,
                      *, client_label: str, timeout: float = 10.0,
                      browser_session: str | None = None) -> dict:
    """Open a transient ws to the running daemon, send one BrowserwrightDaemon.*
    RPC, read the response, close, and return the parsed result (or raise
    with the daemon's error message).

    Used by `open-background`, `close-tab`, `end-session`, `kill-executor`,
    `backend-info`, `extension reload`, and `userscript` subcommands.
    """
    import websockets
    from . import _ipc
    from urllib.parse import quote

    session_q = (
        f"&session={quote(str(browser_session), safe='')}"
        if browser_session else "")

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

    path = _ipc.sock_path()
    if not path.exists():
        raise Unavailable("no daemon running")
    async with websockets.unix_connect(
        str(path),
        uri=f"ws://localhost/?client={client_label}{session_q}",
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
                              *, session: str, timeout: float = 5.0) -> dict:
    params = {**params, "bsSession": session}
    return await _rpc_via_ws(
        cfg, method, params, client_label="cli-userscript", timeout=timeout,
        browser_session=session)


def _cmd_userscript(args, cfg: Config | None = None) -> int:
    if cfg is None:
        cfg = load()
    ns = args
    action = getattr(ns, "userscript_cmd", None)
    if not action:
        print("usage: browserwright-daemon userscript {push,list,remove,toggle,logs} ...",
              file=sys.stderr)
        return 1
    if not getattr(ns, "session", None):
        print("error: provide --session or set BD_SESSION", file=sys.stderr)
        return 2

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
                cfg, "BrowserwrightDaemon.userscript.install",
                {"script": us.to_payload()}, session=ns.session))
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
                cfg, "BrowserwrightDaemon.userscript.list", params,
                session=ns.session))
        elif action == "remove":
            result = _run(_userscript_call_ws(
                cfg, "BrowserwrightDaemon.userscript.remove", {"key": ns.key},
                session=ns.session))
        elif action == "toggle":
            enabled = str(ns.enabled).lower() in {"1", "true", "yes", "on"}
            result = _run(_userscript_call_ws(
                cfg, "BrowserwrightDaemon.userscript.toggle",
                {"key": ns.key, "enabled": enabled}, session=ns.session))
        elif action == "logs":
            # NB: the relay envelope reserves the "id" key for the RPC
            # request id (relay._request overwrites it), so the script-id
            # filter must travel under a non-colliding key.
            params = {"limit": ns.limit}
            if ns.id:
                params["scriptId"] = ns.id
            result = _run(_userscript_call_ws(
                cfg, "BrowserwrightDaemon.userscript.logs", params,
                session=ns.session))
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
    if not args.session:
        print("error: provide --session or set BD_SESSION", file=sys.stderr)
        return 2
    try:
        result = _run(_rpc_via_ws(
            cfg,
            "BrowserwrightDaemon.openBackgroundTab",
            {"url": args.url, "groupName": args.group, "bsSession": args.session},
            client_label="cli-open-background",
            timeout=15.0,
            browser_session=args.session,
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
    if not args.session:
        print("error: provide --session or set BD_SESSION", file=sys.stderr)
        return 2
    if not args.session_id and not args.target_id:
        print("error: provide --session-id or --target-id", file=sys.stderr)
        return 2
    params: dict = {"bsSession": args.session}
    if args.session_id:
        params["sessionId"] = args.session_id
    if args.target_id:
        params["targetId"] = args.target_id
    try:
        result = _run(_rpc_via_ws(
            cfg,
            "BrowserwrightDaemon.closeTab",
            params,
            client_label="cli-close-tab",
            timeout=10.0,
            browser_session=args.session,
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
    """P5: tear down a browserwright session's extension tabs (owned closed,
    borrowed kept). Prints the {closed, kept} JSON result."""
    es_params: dict = {"session": args.session}
    if getattr(args, "group_id", None) is not None:
        es_params["groupId"] = args.group_id
    try:
        result = _run(_rpc_via_ws(
            cfg,
            "BrowserwrightDaemon.endSession",
            es_params,
            client_label="cli-end-session",
            timeout=10.0,
            browser_session=args.session,
        ))
    except Unavailable as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    except DaemonError as e:
        print(f"error: {e}", file=sys.stderr)
        return 3
    print(json.dumps(result, sort_keys=True))
    return 0


def _cmd_kill_executor(args, cfg: Config) -> int:
    """Phase B: reap a session's resident executor (no browser teardown).

    Best-effort by design — a dead daemon / missing executor must not fail
    `session end`, so every failure mode maps to a clean exit code the caller
    tolerates. Prints the `{ok, killed}` JSON result on success."""
    try:
        result = _run(_rpc_via_ws(
            cfg,
            "BrowserwrightDaemon.killExecutor",
            {"session": args.session},
            client_label="cli-kill-executor",
            timeout=10.0,
            browser_session=args.session,
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

# There is exactly one global daemon, so the LaunchAgent has one fixed label
# and one plist (no per-instance name — BD_NAME was removed).
_LAUNCHAGENT_LABEL = "com.browserwright-daemon"


def _launchagent_dir() -> "Path":
    from pathlib import Path
    return Path.home() / "Library" / "LaunchAgents"


def _launchagent_plist_path() -> "Path":
    return _launchagent_dir() / f"{_LAUNCHAGENT_LABEL}.plist"


def _resolve_browserwright_daemon_bin() -> str:
    """Find the absolute path to the `browserwright-daemon` console script. The
    plist needs a fully-qualified path; LaunchAgents don't inherit the
    user's shell PATH."""
    import shutil
    path = shutil.which("browserwright-daemon")
    if path:
        return path
    # Fallback to "<sys.prefix>/bin/browserwright-daemon".
    from pathlib import Path
    candidate = Path(sys.prefix) / "bin" / "browserwright-daemon"
    if candidate.exists():
        return str(candidate)
    raise UserError(
        "browserwright-daemon binary not found on PATH; "
        "install it via pip/uv before running `browserwright-daemon install`"
    )


def _build_plist(*, extension_port: int | None) -> str:
    """Emit the plist content. Kept inline (no XML lib) — the schema is
    fixed + tiny, and we avoid a dependency. Every interpolated value passes
    through ``xml.sax.saxutils.escape``. Pure: no side effects — the caller is
    responsible for creating the log dir.
    """
    bin_path = _resolve_browserwright_daemon_bin()
    args = [bin_path, "serve"]
    if extension_port is not None:
        args += ["--extension-port", str(extension_port)]
    log_dir = os.path.expanduser("~/.cache/browserwright-daemon/logs")
    stdout_path = f"{log_dir}/browserwright-daemon.stdout.log"
    stderr_path = f"{log_dir}/browserwright-daemon.stderr.log"
    label = _LAUNCHAGENT_LABEL
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
              "for Linux run `browserwright-daemon serve` from a systemd-user "
              "unit yourself for now", file=sys.stderr)
        return 1
    if getattr(args, "backend", None):
        print("warning: `browserwright-daemon install --backend` is ignored; "
              "the LaunchAgent runs the single global daemon and session "
              "backends route per session", file=sys.stderr)
    label = _LAUNCHAGENT_LABEL
    plist_path = _launchagent_plist_path()
    if plist_path.exists() and not args.force:
        print(f"error: {plist_path} already exists. "
              f"Use --force to replace.", file=sys.stderr)
        return 1
    plist_path.parent.mkdir(parents=True, exist_ok=True)
    # Create the log dir before launchctl tries to write to it. Kept out of
    # `_build_plist` so the generator stays pure / unit-testable (N-1).
    log_dir = os.path.expanduser("~/.cache/browserwright-daemon/logs")
    os.makedirs(log_dir, exist_ok=True)
    content = _build_plist(extension_port=args.extension_port)
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
        "label": label,
        "plist": str(plist_path),
        "extension_port": args.extension_port,
    }, sort_keys=True))
    return 0


def _cmd_uninstall(args, cfg: Config) -> int:
    if sys.platform != "darwin":
        print("error: `uninstall` is macOS-only", file=sys.stderr)
        return 1
    plist_path = _launchagent_plist_path()
    if not plist_path.exists():
        print(json.dumps({"ok": False,
                          "reason": "no LaunchAgent installed"},
                         sort_keys=True))
        return 0
    rc, _, err = _launchctl("unload", str(plist_path))
    # Even on unload failure (e.g. wasn't loaded), we still remove the plist.
    plist_path.unlink()
    payload = {"ok": True, "removed": str(plist_path)}
    if rc != 0 and err.strip():
        payload["unload_warning"] = err.strip()
    print(json.dumps(payload, sort_keys=True))
    return 0


def _cmd_restart(args, cfg: Config) -> int:
    if sys.platform != "darwin":
        print("error: `restart` is macOS-only (LaunchAgent)", file=sys.stderr)
        return 1
    plist_path = _launchagent_plist_path()
    if not plist_path.exists():
        print(
            "error: no LaunchAgent installed; run `browserwright-daemon install` "
            "or restart your foreground `serve` process manually",
            file=sys.stderr,
        )
        return 2
    _launchctl("unload", str(plist_path))
    rc, _, err = _launchctl("load", "-w", str(plist_path))
    if rc != 0:
        print(f"error: launchctl load failed: {err.strip()}", file=sys.stderr)
        return 3
    print(json.dumps({"ok": True, "restarted": str(plist_path)}, sort_keys=True))
    return 0


def _cmd_list(args, cfg: Config) -> int:
    """Report the single global daemon: whether it's installed as a
    LaunchAgent and whether it's running on the socket."""
    plist_path = _launchagent_plist_path()
    installed = plist_path.exists()
    service = "launchagent" if installed else "manual"
    running_pid = _ipc_ping()
    info = {
        "service": service,
        "plist": str(plist_path) if installed else None,
        "running": running_pid is not None,
        "pid": running_pid,
    }
    if args.json:
        print(json.dumps(info, sort_keys=True))
        return 0
    if not installed and running_pid is None:
        print("no daemon installed or running")
        return 0
    running = "yes" if info["running"] else "no"
    pid = str(running_pid) if running_pid else "-"
    print(f"{'SERVICE':<12} {'RUNNING':<8} {'PID':<8}")
    print(f"{service:<12} {running:<8} {pid:<8}")
    return 0


def _ipc_ping() -> int | None:
    """Tiny wrapper around ipc.ping_sync that swallows everything."""
    try:
        from . import _ipc
        return _ipc.ping_sync(timeout=0.5)
    except Exception:
        return None


_DISPATCH = {
    "doctor": _cmd_doctor,
    "launch-chrome": _cmd_launch_chrome,
    "version": _cmd_version,
    "extension": _cmd_extension,
    # v0.2
    "serve": _cmd_serve,
    "stop": _cmd_stop,
    "restart": _cmd_restart,
    "status": _cmd_status,
    "logs": _cmd_logs,
    # v0.5
    "backend-info": _cmd_backend_info,
    # v0.5.4 — extension backend
    "attach-active": _cmd_attach_active,
    "open-background": _cmd_open_background,
    "close-tab": _cmd_close_tab,
    "end-session": _cmd_end_session,
    "kill-executor": _cmd_kill_executor,
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
