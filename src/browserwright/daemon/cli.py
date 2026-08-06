"""argparse + subcommand dispatch. Nothing else lives here.

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

The work each subcommand names lives in its own module; this file parses, calls
one of them, and formats the answer:

  `_rpc`         one-shot BrowserwrightDaemon.* JSON-RPC over the control socket
  `probe`        daemon liveness observations, behind `status` (and `serve`)
  `supervise`    graceful-then-forced process termination, behind `stop`
  `launchagent`  macOS service registration, behind `install`/`uninstall`/`restart`
  `relay_status` the relay's /__status__ endpoint, behind `version --check`
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from dataclasses import dataclass
from typing import Callable, NoReturn

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
            "(extension relay, cdp, or an env-supplied CDP endpoint)."
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
    # Facade bind host. Defaults to loopback (127.0.0.1) — the facade is NEVER
    # reachable off-box unless you opt in here. Set a Tailscale/LAN IP (or
    # 0.0.0.0) to let a remote Playwright client connect_over_cdp. Equivalent to
    # BD_FACADE_HOST env or `facade_host` in config.toml.
    p_serve.add_argument(
        "--facade-host", type=str, default=None, metavar="HOST",
        help=("bind the Playwright-facing CDP facade on this host "
              "(default: 127.0.0.1 / loopback). Set a Tailscale/LAN IP or "
              "0.0.0.0 to reach it from another machine. Equivalent to "
              "BD_FACADE_HOST env or `facade_host` in config.toml."))

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

    # ps — in-flight introspection. Sibling of `status`, NOT a flag on it:
    # `status` answers "is a daemon there" (liveness, used by the skill layer as
    # a health ping) and must stay cheap and stable. `ps` answers "what is it
    # doing right now", which is a different question with a different failure
    # mode — you run it precisely when `status` already said "alive".
    p_ps = sub.add_parser(
        "ps",
        help="list what the running daemon is waiting on (clients, in-flight "
             "requests, executors)")
    p_ps.add_argument("--json", action="store_true",
                      help="emit the raw status payload instead of tables")
    p_ps.add_argument("--timeout", type=float, default=5.0,
                      help="seconds to wait for the daemon to answer")

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
    p_inst.add_argument("--facade-port", type=int, default=None, metavar="N",
                        help="override the Playwright facade port (default 19990)")
    p_inst.add_argument(
        "--facade-host", type=str, default=None, metavar="HOST",
        help=("bind the Playwright facade on this host (default 127.0.0.1 / "
              "loopback). Set a Tailscale/LAN IP or 0.0.0.0 to reach the "
              "installed daemon's facade from another machine."))
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


def _backend_name(value: str) -> str:
    """`--backend` value, rejecting the retired names with a migration hint.

    A `type=` callable rather than `choices=`, because argparse's own
    "invalid choice: 'rdp' (choose from 'cdp', 'extension')" tells a user with
    `--backend=rdp` in a script that they are wrong without telling them what
    to write — and for `env` the answer isn't even a rename, it's a per-session
    `--attach` (#38).
    """
    if value in names():
        return value
    from .config import retired_backend_message

    retired = retired_backend_message(value)
    if retired is not None:
        raise argparse.ArgumentTypeError(retired)
    raise argparse.ArgumentTypeError(
        f"invalid backend {value!r} (choose from {', '.join(names())})")


def _add_common(sp: argparse.ArgumentParser) -> None:
    sp.add_argument("--backend", type=_backend_name, metavar="NAME",
                    help=("pin backend for this command; for `serve`, this only "
                          "chooses the shared upstream while session backends "
                          "still route per session"))
    sp.add_argument("--timeout", type=float, default=None,
                    help="per-backend timeout in seconds (default 5)")
    sp.add_argument("--config", help="optional toml config path; otherwise reads BD_CONFIG")
    sp.add_argument("-v", "--verbose", action="store_true")


def _add_port(sp: argparse.ArgumentParser) -> None:
    sp.add_argument("--port", type=int, default=None,
                    help="cdp backend port (default 9222 / config-backends.cdp.port)")


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
        cli_facade_host=getattr(args, "facade_host", None),
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
    with the always-on relay. cdp sessions get their own per-session upstream
    on top, dispatched by the ledger's immutable per-session backend. The old
    "fail loud on missing backend (to avoid a silent cdp fallback)" guard is
    gone: there is no single-backend lifetime to protect anymore.
    """
    from .server.listener import run_serve
    return _run(run_serve(cfg))


def _cmd_stop(args, cfg: Config) -> int:
    """Stop the running daemon: SIGTERM, wait, SIGKILL.

    Exits 0 for every outcome the user can act on — already stopped, stopped
    now, or refused because the pid was recycled (in which case there is nothing
    left to stop). Only an OS refusal to signal at all exits 3.

    We do NOT trust the pid file alone — we ping first to verify it's our
    daemon, then signal that pid. (Mirrors browser-harness `_ipc.identify`.)

    PID-reuse guard: between the ping and the kill the daemon could die and the
    OS recycle its pid for an unrelated process. We fingerprint the pid's
    process start-time and hand that check to `supervise.terminate` as its
    `guard`, which re-verifies before each signal. When the platform can't
    report a start-time (``proc_start_time`` → None) the guard passes, so an
    unverifiable platform degrades to "signal anyway" rather than blocking a
    legitimate stop.

    Death is measured by the *ping*, not by the process table: a daemon that has
    stopped answering has stopped serving, which is all `stop` promises.
    """
    from . import _ipc, platforms, supervise

    pid = _ipc.ping_sync(timeout=1.0)
    if pid is None:
        # No live daemon. Still clean up stale files so the next `serve` can
        # bind freshly without manual intervention.
        _ipc.cleanup_endpoint()
        print("no live daemon; cleaned up stale files", file=sys.stderr)
        return 0

    start0 = platforms.proc_start_time(pid)

    def _same_process() -> bool:
        if start0 is None:
            return True
        return platforms.proc_start_time(pid) == start0

    outcome = supervise.terminate(
        pid,
        is_dead=lambda: _ipc.ping_sync(timeout=0.3) is None,
        grace=args.timeout,
        # Nothing left to do after the SIGKILL but clean up and exit, so don't
        # spend another ping confirming it.
        kill_grace=0,
        interval=0.1,
        guard=_same_process,
    )
    if outcome is supervise.Outcome.EXITED:
        # Graceful shutdown cleans up its own socket/pid/facade files.
        return 0
    if outcome is supervise.Outcome.SIGNAL_FAILED:
        print(f"error: cannot signal daemon pid {pid} (not permitted?); "
              f"stop it manually", file=sys.stderr)
        return 3
    if outcome is supervise.Outcome.REFUSED:
        print(f"daemon pid {pid} was recycled by another process; not "
              f"signalling it", file=sys.stderr)
    elif outcome is supervise.Outcome.REFUSED_ESCALATION:
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


def _cmd_status(args, cfg: Config, *, probe=None) -> int:
    """Report endpoint + liveness. JSON shape used by Skill for status pings.

    `probe` overrides the side-effecting observations (see `probe.DaemonProbe`);
    production passes nothing. This is the daemon *existence* check — in-flight
    introspection is a separate subcommand, not a field here.
    """
    from .probe import PORT_HELD, daemon_status

    st = daemon_status(cfg, probe=probe)
    if args.json:
        print(json.dumps(st.to_dict(), sort_keys=True))
    elif st.alive:
        print(f"daemon alive (pid {st.pid})")
        print(f"  socket: {st.endpoint['path']}")
        if st.facade:
            print(f"  facade: {st.facade['ws']}")
    elif st.probe_state == PORT_HELD:
        held = f" (pid {st.port_holder_pid})" if st.port_holder_pid else ""
        print("daemon unresponsive but holding its ports"
              f"{held} — a half-alive daemon.")
        print("  fix: `browserwright-daemon restart` reclaims the "
              "ports; or kill the pid above manually.")
    else:
        print("daemon not running")
    return 0 if st.alive else 2


def _cmd_ps(args, cfg: Config) -> int:
    """Print what the daemon is currently waiting on.

    Three tables side by side — router hop, relay hop, executor hop — because
    each hop keeps its own request ids in its own id space. Correlating them is
    left to the reader's eyes (method name + elapsed), which is enough for a
    local daemon where concurrent in-flight requests are a single-digit number,
    and avoids threading a synthetic request id through five id spaces and the
    executor wire format to get it.
    """
    def _emit(payload: dict) -> None:
        if args.json:
            print(json.dumps(payload, sort_keys=True))
        else:
            _pretty_ps(payload)

    return _rpc_cmd(cfg, "BrowserwrightDaemon.status", {},
                    client_label="cli-ps", timeout=args.timeout, emit=_emit)


def _cmd_attach_active(args, cfg: Config) -> int:
    """Ask the running daemon (extension backend) to attach the
    currently-focused-window active tab. Prints the result as JSON or as
    `targetId<TAB>url<TAB>title`. Exits 1 if the daemon errored.
    """
    if not args.session:
        print("error: provide --session or set BD_SESSION", file=sys.stderr)
        return 2
    try:
        result = _run(_rpc_via_ws(
            cfg, "BrowserwrightDaemon.attachActiveTab",
            {"bsSession": args.session},
            client_label="cli-attach-active", timeout=15.0,
            browser_session=args.session,
        ))
    except Unavailable as e:
        print(f"{e}", file=sys.stderr)
        return 2
    except Exception as e:
        # Historic contract: any attach failure (daemon error response or
        # transport hiccup) exits 1, not main()'s generic 3.
        print(f"attach-active failed: {e}", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(result, sort_keys=True))
    else:
        print(f"{result.get('targetId')}\t{result.get('url', '')}\t"
              f"{result.get('title', '')}")
    return 0


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
    from .relay_status import fetch_json

    host, port = cfg.backends.extension.resolved_host_port()
    return fetch_json(host, port)


def _cmd_extension(args, cfg: Config) -> int:
    action = getattr(args, "extension_cmd", None)
    if action == "reload":
        def _emit(result: dict) -> None:
            if args.json:
                print(json.dumps(result, sort_keys=True))
            else:
                sent = int(result.get("sent", 0) or 0)
                print(f"reload requested for {sent} extension(s)")

        return _rpc_cmd(
            cfg, "BrowserwrightDaemon.extension.reload", {"reason": "manual"},
            client_label="cli-extension-reload", timeout=8.0, emit=_emit)
    print("usage: browserwright-daemon extension reload", file=sys.stderr)
    return 1


async def _rpc_via_ws(cfg: Config, method: str, params: dict,
                      *, client_label: str, timeout: float = 10.0,
                      browser_session: str | None = None) -> dict:
    """Module-local seam over `_rpc.call` (see that module for the protocol).

    Kept as a name in this module so every handler — and every test that fakes
    the daemon — has exactly one place to hook, instead of each handler reaching
    into `_rpc` behind its own lazy import.
    """
    from . import _rpc
    return await _rpc.call(cfg, method, params, client_label=client_label,
                           timeout=timeout, browser_session=browser_session)


def _rpc_cmd(cfg: Config, method: str, params: dict, *,
             client_label: str, timeout: float = 10.0,
             browser_session: str | None = None,
             emit=None, validate_result=None) -> int:
    """Shared runner for one-shot RPC subcommands: call `_rpc_via_ws`, map
    Unavailable→2 / DaemonError→3 — the same mapping main()'s top-level
    handler applies, duplicated here because tests (and any embedder) invoke
    the `_cmd_*` handlers directly without going through main(). On success
    prints the sorted-JSON result (spec §5.1 single-line discipline) unless
    `emit` overrides the formatting."""
    try:
        result = _run(_rpc_via_ws(
            cfg, method, params, client_label=client_label, timeout=timeout,
            browser_session=browser_session,
        ))
        if validate_result is not None:
            validate_result(result)
    except Unavailable as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    except DaemonError as e:
        print(f"error: {e}", file=sys.stderr)
        return 3
    if emit is not None:
        emit(result)
    else:
        print(json.dumps(result, sort_keys=True))
    return 0


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


# ---- pure-forwarding subcommands -------------------------------------------
#
# These subcommands do nothing but name a verb and shape its params, so they are
# a table rather than four near-identical functions: adding the fifth is one row,
# and `_FORWARDS` doubles as the auditable inventory of which CLI surface maps to
# which `BrowserwrightDaemon.*` verb. Anything with bespoke output
# (`attach-active`'s tab-separated form) or bespoke error mapping stays a real
# handler above — the table is for forwarding, not for "almost forwarding".
#
# end-session is deliberately NOT a row: it is the one forwarding-shaped verb
# whose worst case outlives the CLI timeout (issue #32), so it has a real
# handler (`_cmd_end_session`) implementing the initiate-then-join contract.


@dataclass(frozen=True)
class _Forward:
    """One `(subcommand → verb)` row."""

    method: str
    label: str
    timeout: float
    #: Build the RPC params from the parsed args.
    params: Callable[[argparse.Namespace], dict]
    #: Reject a bad arg combination before opening a socket. Returns the error
    #: line to print (→ exit 2), or None to proceed.
    precheck: Callable[[argparse.Namespace], str | None] | None = None
    #: Reject a technically-successful response that doesn't mean what the
    #: caller needs. Raises DaemonError (→ exit 3).
    validate: Callable[[dict], None] | None = None


def _need_session(a) -> str | None:
    return None if a.session else "error: provide --session or set BD_SESSION"


def _need_tab_ref(a) -> str | None:
    return _need_session(a) or (
        None if (a.session_id or a.target_id)
        else "error: provide --session-id or --target-id")


def _close_tab_params(a) -> dict:
    return {"bsSession": a.session,
            **({"sessionId": a.session_id} if a.session_id else {}),
            **({"targetId": a.target_id} if a.target_id else {})}


def _end_session_params(a) -> dict:
    gid = getattr(a, "group_id", None)
    return {"session": a.session,
            **({"groupId": gid} if gid is not None else {})}


def _require_reaped(result: dict) -> None:
    """`kill-executor` is idempotent, but a wait-mode response only counts when
    the daemon confirms ``reaped: true``. `session end` may treat a nonzero exit
    as best-effort; `session reset` relies on this to avoid claiming a live
    executor was recycled."""
    if result.get("reaped") is not True:
        raise DaemonError(
            "BrowserwrightDaemon.killExecutor did not confirm executor "
            f"death: {result!r}")


def _require_complete_end_session(result: dict) -> None:
    """A partial extension close is not a successful workspace teardown."""
    if result.get("ok") is not True:
        raise DaemonError(
            "BrowserwrightDaemon.endSession left tabs open: "
            f"{result!r}")


#: Issue #32 initiate-then-join contract. The initiate RPC only does the
#: bounded fast phase (revoke + reap) and returns in well under a second; the
#: unbounded workspace teardown continues daemon-side. The join RPC blocks
#: until the teardown finishes (or the daemon restarts and re-initiates), so
#: its per-attempt timeout covers the whole daemon-side worst case.
_END_SESSION_INITIATE_TIMEOUT = 10.0
_END_SESSION_JOIN_TIMEOUT = 20.0
#: Total wall-clock budget for `session end` to reach the final result before
#: giving up. Generous on purpose: progress is printed while waiting, so a
#: slow teardown is visible, and a teardown still running after this long is
#: wedged regardless of backend.
_END_SESSION_TOTAL_WAIT_S = 90.0


def _end_session_rpc(cfg: Config, params: dict, session: str,
                     timeout: float) -> dict:
    """One `endSession` RPC (initiate or join). Raises `TimeoutError` when
    the daemon does not answer in time — never a fake result."""
    return _run(_rpc_via_ws(
        cfg, "BrowserwrightDaemon.endSession", params,
        client_label="cli-end-session", timeout=timeout,
        browser_session=session))


def _cmd_end_session(args, cfg: Config) -> int:
    """P5 teardown under the issue #32 initiate-then-join contract.

    The first call initiates: the daemon revokes clients, reaps the executor,
    publishes phase=terminating, and returns immediately while the unbounded
    workspace teardown keeps running daemon-side. This handler then re-issues
    `endSession` to JOIN the in-flight teardown, printing progress to stderr
    so a slow teardown is distinguishable from a hung daemon. Exit 0 only
    when the daemon confirms the workspace teardown completed; the ledger
    entry is removed by Layer 2 only on that exit code.

    Against an old daemon (no initiate contract) the first response is
    already the final result, and the poll loop is skipped."""
    params = _end_session_params(args)
    deadline = time.monotonic() + _END_SESSION_TOTAL_WAIT_S
    started = time.monotonic()
    result: dict | None = None
    try:
        try:
            result = _end_session_rpc(
                cfg, params, args.session, _END_SESSION_INITIATE_TIMEOUT)
        except TimeoutError:
            # A previous teardown may still be joining; enter the poll loop.
            pass
        while result is None or result.get("initiated") is True:
            if time.monotonic() >= deadline:
                raise DaemonError(
                    "BrowserwrightDaemon.endSession is still terminating "
                    f"after {_END_SESSION_TOTAL_WAIT_S:.0f}s; watch progress "
                    "with `browserwright-daemon ps`")
            print(
                f"session {args.session}: tearing down… "
                f"({time.monotonic() - started:.0f}s)",
                file=sys.stderr, flush=True)
            try:
                result = _end_session_rpc(
                    cfg, params, args.session, _END_SESSION_JOIN_TIMEOUT)
            except TimeoutError:
                result = None  # join still running daemon-side; keep polling
                continue
            _require_complete_end_session(result)
    except Unavailable as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    except DaemonError as e:
        print(f"error: {e}", file=sys.stderr)
        return 3
    print(json.dumps(result, sort_keys=True))
    return 0


#: Every forwarding subcommand. All emit the sorted-JSON RPC result verbatim
#: (spec §5.1 single-line discipline) — these responses are structured, so a
#: tab-separated form would lose fields. Each passes `browser_session`, so the
#: transient ws carries `?session=`; without it the daemon's
#: `_require_browser_session` rejects the connection before the verb ever runs.
_FORWARDS: dict[str, _Forward] = {
    "open-background": _Forward(
        "BrowserwrightDaemon.openBackgroundTab", "cli-open-background", 15.0,
        lambda a: {"url": a.url, "groupName": a.group, "bsSession": a.session},
        precheck=_need_session),
    "close-tab": _Forward(
        "BrowserwrightDaemon.closeTab", "cli-close-tab", 10.0,
        _close_tab_params, precheck=_need_tab_ref),
    # P5 teardown: close owned tabs, keep borrowed. `--session` is
    # argparse-required here, hence no precheck.
    #
    # NOTE: `end-session` is deliberately NOT a row (issue #32) — it is the
    # one forwarding-shaped verb whose worst case outlives the CLI timeout,
    # so it has a real handler (`_cmd_end_session`) implementing the
    # initiate-then-join contract below.
    # Phase B: reap a session's resident executor, no browser teardown.
    "kill-executor": _Forward(
        "BrowserwrightDaemon.killExecutor", "cli-kill-executor", 10.0,
        lambda a: {"session": a.session, "wait": True},
        validate=_require_reaped),
}


def _forwarding_handler(spec: _Forward):
    def handler(args, cfg: Config) -> int:
        if spec.precheck is not None and (err := spec.precheck(args)):
            print(err, file=sys.stderr)
            return 2
        return _rpc_cmd(cfg, spec.method, spec.params(args),
                        client_label=spec.label, timeout=spec.timeout,
                        browser_session=args.session,
                        validate_result=spec.validate)
    handler.__name__ = f"_cmd_{spec.label.removeprefix('cli-').replace('-', '_')}"
    return handler


# ---- LaunchAgent service (macOS) -------------------------------------------
#
# The operations live in `launchagent.py`; these three handlers only translate
# its return values / LaunchAgentError into stdout, stderr and exit codes.


def _cmd_install(args, cfg: Config) -> int:
    from . import launchagent

    def op() -> dict:
        # Platform first: on Linux the `--backend` warning would be noise on top
        # of a hard "macOS-only" refusal.
        launchagent.require_darwin("install")
        if getattr(args, "backend", None):
            print("warning: `browserwright-daemon install --backend` is "
                  "ignored; the LaunchAgent runs the single global daemon and "
                  "session backends route per session", file=sys.stderr)
        return launchagent.install(
            extension_port=args.extension_port,
            facade_host=getattr(args, "facade_host", None),
            facade_port=getattr(args, "facade_port", None),
            force=args.force,
        )

    return _launchagent_cmd(op)


def _cmd_uninstall(args, cfg: Config) -> int:
    from . import launchagent
    return _launchagent_cmd(launchagent.uninstall)


def _cmd_restart(args, cfg: Config) -> int:
    from . import launchagent
    return _launchagent_cmd(launchagent.restart)


def _launchagent_cmd(op) -> int:
    """Run a `launchagent` operation; print its report or its error."""
    from .launchagent import LaunchAgentError
    try:
        report = op()
    except LaunchAgentError as e:
        print(f"error: {e}", file=sys.stderr)
        return e.exit_code
    print(json.dumps(report, sort_keys=True))
    return 0


def _cmd_list(args, cfg: Config) -> int:
    """Report the single global daemon: whether it's installed as a
    LaunchAgent and whether it's running on the socket."""
    from . import launchagent
    plist_path = launchagent.plist_path()
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
    "ps": _cmd_ps,
    "logs": _cmd_logs,
    # v0.5
    "backend-info": _cmd_backend_info,
    # v0.5.4 — extension backend
    "attach-active": _cmd_attach_active,
    "userscript": _cmd_userscript,
    # v0.5.5 — LaunchAgent service (macOS) so the daemon is long-running.
    "install": _cmd_install,
    "uninstall": _cmd_uninstall,
    "list": _cmd_list,
    # open-background / close-tab / end-session / kill-executor
    "end-session": _cmd_end_session,  # issue #32: real handler, not a row
    **{name: _forwarding_handler(spec) for name, spec in _FORWARDS.items()},
}


# ---- pretty print ----------------------------------------------------------


def _secs(v) -> str:
    """Render a duration. `-` when the source had no timestamp to offer — an
    absent clock must not be printed as `0.0s`, which is a different claim."""
    if not isinstance(v, (int, float)):
        return "-"
    if v < 60:
        return f"{v:.1f}s"
    if v < 3600:
        return f"{int(v // 60)}m{int(v % 60):02d}s"
    return f"{int(v // 3600)}h{int((v % 3600) // 60):02d}m"


def _pretty_ps(p: dict) -> None:
    d = p.get("daemon") or {}
    print(f"daemon pid {d.get('pid')} version {d.get('version')} "
          f"schema {p.get('schema_version')}")

    empty = True
    for ctx in p.get("contexts") or []:
        label = ctx.get("session_id") or "(shared)"
        print(f"\ncontext {label}  backend={ctx.get('backend')} "
              f"upstream={ctx.get('upstream_phase')} "
              f"idle={_secs(ctx.get('idle_s'))} "
              f"targets={ctx.get('targets')} attachers={ctx.get('attachers')}")
        clients = ctx.get("clients") or []
        if clients:
            print(f"  {'CLIENT':<8} {'LABEL':<22} {'SESSION':<20} "
                  f"{'SESS':<5} {'BUF':<4} {'CONNECTED':<10} {'LAST CMD':<10}")
            for c in clients:
                print(f"  {str(c.get('client_id')):<8} "
                      f"{str(c.get('label') or '')[:22]:<22} "
                      f"{str(c.get('session_id') or '-')[:20]:<20} "
                      f"{c.get('sessions', 0):<5} "
                      f"{c.get('pre_open_buffered', 0):<4} "
                      f"{_secs(c.get('connected_age_s')):<10} "
                      f"{_secs(c.get('last_command_age_s')):<10}")
        for r in ctx.get("pending_requests") or []:
            empty = False
            print(f"  IN-FLIGHT  hop=router  method={r.get('method')}  "
                  f"elapsed={_secs(r.get('elapsed_s'))}  "
                  f"client={r.get('client_id')}  upstream_id={r.get('upstream_id')}")

    relay = p.get("relay") or {}
    if relay.get("running"):
        exts = relay.get("extensions") or []
        print(f"\nrelay  extensions={len(exts)}")
        for e in exts:
            print(f"  {e.get('install_id') or '?'}  "
                  f"version={e.get('browserwright_version') or '?'}  "
                  f"pending={e.get('pending', 0)}  "
                  f"oldest={_secs(e.get('oldest_pending_s'))}")
        for r in relay.get("inflight") or []:
            empty = False
            name = r.get("method") or r.get("kind")
            print(f"  IN-FLIGHT  hop=relay  method={name}  "
                  f"elapsed={_secs(r.get('elapsed_s'))}  "
                  f"tab={r.get('tab_id')}  id={r.get('id')}")

    sessions = p.get("sessions") or []
    if sessions:
        print(f"\nsessions  {len(sessions)}")
        print(f"  {'SESSION':<22} {'PHASE':<12} RESULT")
        for s in sessions:
            result = s.get("result") or {}
            ok = result.get("ok")
            summary = ""
            if isinstance(ok, bool):
                summary = (f"ok={ok} closed={len(result.get('closed') or [])} "
                           f"failed={result.get('failed') or []}")
            print(f"  {str(s.get('session_id'))[:22]:<22} "
                  f"{str(s.get('phase') or 'active')[:12]:<12} {summary}")

    executors = p.get("executors") or []
    print(f"\nexecutors  {len(executors)}")
    if executors:
        print(f"  {'SESSION':<22} {'PID':<8} {'ALIVE':<6} {'AGE':<10} {'IDLE':<10}")
    for e in executors:
        print(f"  {str(e.get('session_id'))[:22]:<22} "
              f"{str(e.get('pid')):<8} "
              f"{('yes' if e.get('alive') else 'no'):<6} "
              f"{_secs(e.get('age_s')):<10} {_secs(e.get('idle_s')):<10}")
        fl = e.get("inflight")
        if fl:
            empty = False
            print(f"    IN-FLIGHT  hop=executor  what={fl.get('what')}  "
                  f"elapsed={_secs(fl.get('elapsed_s'))}  "
                  f"request={fl.get('request_id')}  "
                  f"budget={fl.get('timeout_ms')}ms")

    if empty:
        print("\nnothing in flight")


def _pretty_doctor(out: dict) -> None:
    # v3 (issue #28): lead with daemon liveness so a down daemon — the one
    # condition that makes every backend unavailable — is never buried.
    if out.get("alive"):
        print(f"daemon: alive (pid {out.get('pid')})")
    else:
        print(f"daemon: not running (probe_state={out.get('probe_state')})")
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
