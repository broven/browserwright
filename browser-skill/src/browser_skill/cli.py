"""Top-level ``browser-skill`` CLI dispatch.

Subcommands:
  session new | end | list | prune        (P2: explicit session creation)
  whoami --session=ID
  task <site>/<name> [--arg=val ...]    NOT IN v0.1 ENTRY: minimal stub
  install
  doctor
  save <site>/<name>                    proxy to solidify with a spec dict
  list-tasks [--site SITE]
  index rebuild
  memory show [--site SITE | --global]
  version
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Optional

from . import __version__


HELP = """browser-skill — Layer 2 of the browser stack.

Usage:
  browser-skill <<'PY'
  print(page_info())
  PY

  browser-skill session new --backend=<extension|rdp> --name=NAME [--create | --attach=PORT]
  browser-skill session end --session=ID
  browser-skill session list [--json]
  browser-skill session prune [--idle=SECONDS]
  browser-skill whoami --session=ID

  browser-skill task <site>/<name> [--key=value ...] [--isolated]
  browser-skill list-tasks [--site SITE] [--query Q] [--json]
  browser-skill save <site>/<name> --json-spec='{...}'      (alias: solidify)

  browser-skill selftest run [--site SITE] [--isolated] [--json]

  browser-skill sub add <git-url> [--name NAME]
  browser-skill sub list [--json]
  browser-skill sub update [--name NAME]
  browser-skill sub remove --name NAME

  browser-skill install
  browser-skill doctor [--json]
  browser-skill index rebuild
  browser-skill memory show [--site SITE | --global]
  browser-skill memory forget --pattern PAT (--site SITE | --global) [--yes]
  browser-skill memory replace --pattern PAT --with 'TEXT' (--site SITE | --global) [--yes]

  browser-skill version
"""


def _parse_kv_args(args: list[str]) -> dict:
    out: dict[str, object] = {}
    for a in args:
        if not a.startswith("--"):
            continue
        key, _, value = a[2:].partition("=")
        if not _:
            out[key] = True
        else:
            # try JSON first so callers can pass numbers/lists/etc.
            try:
                out[key] = json.loads(value)
            except (TypeError, ValueError):
                out[key] = value
    return out


def _cmd_task(args: list[str]) -> int:
    if not args:
        print("usage: browser-skill task <site>/<name> [--key=val ...]", file=sys.stderr)
        return 1
    spec = args[0]
    if "/" not in spec:
        print("task spec must be <site>/<name>", file=sys.stderr)
        return 1
    site, name = spec.split("/", 1)
    kwargs = _parse_kv_args(args[1:])
    # JSON-args envelope for Layer 3 callers.
    js = kwargs.pop("json-args", None)
    if js is not None:
        if isinstance(js, str):
            kwargs.update(json.loads(js))
        elif isinstance(js, dict):
            kwargs.update(js)
    from .task_runner import run_task
    try:
        result = run_task(site, name, **kwargs)
    except FileNotFoundError as e:
        print(f"task not found: {e}", file=sys.stderr)
        return 1
    except Exception as e:  # noqa: BLE001
        print(f"task crashed: {e!r}", file=sys.stderr)
        return 3
    if "--json-output" in args or kwargs.get("json_output"):
        sys.stdout.write(json.dumps(result, default=str))
    else:
        sys.stdout.write(repr(result))
    sys.stdout.write("\n")
    return 0


def _cmd_doctor(args: list[str]) -> int:
    from .daemon_client import DaemonClient

    cli = DaemonClient()
    info = cli.doctor()
    info["skill_version"] = __version__
    if "--json" in args:
        sys.stdout.write(json.dumps(info, indent=2, default=str) + "\n")
        return 0
    print(f"browser-skill {__version__}")
    print()
    backends = info.get("backends", [])
    if not backends:
        print("(no backend info — is browser-daemon installed?)")
        if info.get("error"):
            print(f"  error: {info['error']}")
        return 0
    for b in backends:
        name = b.get("name", "?")
        avail = "✓" if b.get("available") else "✗"
        cost = b.get("ux_cost", "?")
        ws_url = b.get("ws_url", "")
        print(f"  {avail} {name:14s} ux_cost={cost} ws={ws_url}")
        if b.get("ux_warning"):
            print(f"     warning: {b['ux_warning']}")
        if b.get("needs_user_action"):
            print(f"     ! {b['needs_user_action']}")
    return 0


def _cmd_install(_: list[str]) -> int:
    from . import install
    return install.run()


def _cmd_list_tasks(args: list[str]) -> int:
    kwargs = _parse_kv_args(args)
    from .discovery import list_tasks
    tasks = list_tasks(site=kwargs.get("site"), query=kwargs.get("query"))
    if kwargs.get("json"):
        sys.stdout.write(json.dumps(tasks, default=str) + "\n")
        return 0
    if not tasks:
        print("(no tasks found)")
        return 0
    for t in tasks:
        print(f"  {t['site']}/{t['name']}  — {t.get('desc','')}")
    return 0


def _cmd_save(args: list[str]) -> int:
    """``save <site>/<name> --json-spec='{...}'``: persist a solidify spec."""
    if not args:
        print("usage: browser-skill save <site>/<name> --json-spec='{...}'",
              file=sys.stderr)
        return 1
    spec_arg = args[0]
    kwargs = _parse_kv_args(args[1:])
    if "/" not in spec_arg:
        print("save target must be <site>/<name>", file=sys.stderr)
        return 1
    site, name = spec_arg.split("/", 1)
    spec_text = kwargs.get("json-spec")
    if not spec_text:
        # read from stdin
        spec_text = sys.stdin.read().strip()
    if not spec_text:
        print("missing --json-spec=... or stdin", file=sys.stderr)
        return 1
    if isinstance(spec_text, str):
        spec = json.loads(spec_text)
    else:
        spec = spec_text
    spec.setdefault("site", site)
    spec.setdefault("suggested_name", name)
    from .session import current_session
    from .solidify import scaffold
    result = scaffold.commit(current_session(), spec)
    sys.stdout.write(json.dumps(result, default=str) + "\n")
    return 0


def _cmd_index(args: list[str]) -> int:
    if args and args[0] == "rebuild":
        from .discovery import rebuild_index
        out = rebuild_index()
        sys.stdout.write(json.dumps({"sites": len(out.get("sites", []))}, default=str) + "\n")
        return 0
    print("usage: browser-skill index rebuild", file=sys.stderr)
    return 1


def _cmd_sub(args: list[str]) -> int:
    """``browser-skill sub {add|list|update|remove} ...``."""
    if not args:
        print("usage: browser-skill sub {add|list|update|remove} ...", file=sys.stderr)
        return 1
    sub = args[0]
    rest = args[1:]
    from . import subscriptions

    if sub == "add":
        if not rest or rest[0].startswith("--"):
            print("usage: browser-skill sub add <git-url> [--name NAME]", file=sys.stderr)
            return 1
        url = rest[0]
        kw = _parse_kv_args(rest[1:])
        try:
            r = subscriptions.add(url, name=kw.get("name"))
        except subscriptions.SubscriptionError as e:
            print(f"sub add failed: {e}", file=sys.stderr)
            return 1
        if kw.get("json"):
            sys.stdout.write(json.dumps(r, default=str) + "\n")
        else:
            print(f"{r['status']}: {r['name']} → {r['path']}")
        return 0

    if sub == "list":
        kw = _parse_kv_args(rest)
        rows = subscriptions.list_all()
        if kw.get("json"):
            sys.stdout.write(json.dumps(rows, indent=2, default=str) + "\n")
            return 0
        if not rows:
            print("(no subscriptions)")
            return 0
        for r in rows:
            tag = "" if r["exists"] else " [MISSING]"
            print(f"  {r['name']:24s} {r['url']}{tag}")
        return 0

    if sub == "update":
        kw = _parse_kv_args(rest)
        names = [kw["name"]] if kw.get("name") else None
        try:
            results = subscriptions.update(names)
        except subscriptions.SubscriptionError as e:
            print(f"sub update failed: {e}", file=sys.stderr)
            return 1
        for r in results:
            print(f"  {r['name']:24s} {r['status']}: {r.get('detail','')}")
        return 0 if all(r["status"] in ("updated", "missing") for r in results) else 1

    if sub == "remove":
        kw = _parse_kv_args(rest)
        name = kw.get("name")
        if not name:
            print("usage: browser-skill sub remove --name NAME", file=sys.stderr)
            return 1
        try:
            subscriptions.remove(name)
        except subscriptions.SubscriptionError as e:
            print(f"sub remove failed: {e}", file=sys.stderr)
            return 1
        print(f"removed {name}")
        return 0

    print(f"unknown sub subcommand: {sub}", file=sys.stderr)
    return 1


def _cmd_selftest(args: list[str]) -> int:
    """``browser-skill selftest run [--site SITE] [--isolated] [--json]``.

    Refreshes ``$BS_HOME/selftest_cache.json`` for every (or filtered) task.
    Exit code is 0 if all tasks pass, 1 if any failure/error, 2 if no tasks
    were discovered.
    """
    if not args or args[0] != "run":
        print("usage: browser-skill selftest run [--site SITE] [--isolated] [--json]",
              file=sys.stderr)
        return 1
    kwargs = _parse_kv_args(args[1:])
    from .selftest_runner import run_all
    summary = run_all(
        site=kwargs.get("site"),
        isolated=bool(kwargs.get("isolated", False)),
    )
    if kwargs.get("json"):
        sys.stdout.write(json.dumps(summary, indent=2, default=str) + "\n")
    else:
        totals = summary["totals"]
        print(f"selftest done in {summary['duration_sec']}s — "
              f"ok={totals['ok']} fail={totals['fail']} "
              f"skip={totals['skip']} error={totals['error']}")
        for r in summary["results"]:
            mark = {"ok": "✓", "fail": "✗", "error": "!", "skip": "·"}.get(r["verdict"], "?")
            print(f"  {mark} {r['site']}/{r['name']:20s} — {r.get('reason','')}")
    if not summary["results"]:
        return 2
    if summary["totals"]["fail"] or summary["totals"]["error"]:
        return 1
    return 0


def _cmd_memory(args: list[str]) -> int:
    if not args:
        print("usage: browser-skill memory {show|forget|replace} ...", file=sys.stderr)
        return 1
    sub = args[0]
    rest = args[1:]
    kwargs = _parse_kv_args(rest)
    from .memory import global_memory, site_memory

    if sub == "show":
        if kwargs.get("global"):
            out = global_memory().read()
            sys.stdout.write(json.dumps(out, indent=2, default=str) + "\n")
            return 0
        site = kwargs.get("site")
        if not site:
            print("specify --site=SITE or --global", file=sys.stderr)
            return 1
        sys.stdout.write(json.dumps(site_memory(site).read(),
                                    indent=2, default=str) + "\n")
        return 0

    if sub == "forget":
        pattern = kwargs.get("pattern")
        if not pattern:
            print("usage: memory forget --pattern=PAT (--site=SITE | --global) [--yes]",
                  file=sys.stderr)
            return 1
        target_global = bool(kwargs.get("global"))
        site = kwargs.get("site")
        if not target_global and not site:
            print("specify --site=SITE or --global", file=sys.stderr)
            return 1
        mem = global_memory() if target_global else site_memory(site)
        matches = mem.forget(pattern, confirm=True)
        if not matches:
            print("(no matching bullets)")
            return 0
        print(f"would remove {len(matches)} line(s):")
        for ln in matches:
            print(f"  {ln}")
        if not kwargs.get("yes"):
            print("\nrun again with --yes to confirm.")
            return 0
        removed = mem.forget(pattern, confirm=False)
        print(f"removed {len(removed)} line(s).")
        return 0

    if sub == "replace":
        pattern = kwargs.get("pattern")
        replacement = kwargs.get("with")
        if not pattern or not replacement:
            print("usage: memory replace --pattern=PAT --with='new line' "
                  "(--site=SITE | --global) [--yes]", file=sys.stderr)
            return 1
        target_global = bool(kwargs.get("global"))
        site = kwargs.get("site")
        if not target_global and not site:
            print("specify --site=SITE or --global", file=sys.stderr)
            return 1
        mem = global_memory() if target_global else site_memory(site)
        matches = mem.forget(pattern, confirm=True)
        if not matches:
            print("(no matching bullets)")
            return 0
        print(f"would remove {len(matches)} line(s) and append: - {replacement}")
        for ln in matches:
            print(f"  {ln}")
        if not kwargs.get("yes"):
            print("\nrun again with --yes to confirm.")
            return 0
        mem.forget(pattern, confirm=False)
        mem.append(replacement)
        print("replaced.")
        return 0

    print(f"unknown memory subcommand: {sub}", file=sys.stderr)
    return 1


def _cmd_session(args: list[str]) -> int:
    """``browser-skill session {new|end|list|prune} ...`` (P2)."""
    from . import session_create
    from . import session_registry as reg

    if not args:
        print("usage: browser-skill session {new|end|list|prune} ...", file=sys.stderr)
        return 1
    sub = args[0]
    kw = _parse_kv_args(args[1:])

    if sub == "new":
        backend = kw.get("backend")
        if backend not in ("extension", "rdp"):
            print("usage: browser-skill session new --backend=<extension|rdp> "
                  "--name=NAME [--create | --attach=PORT]", file=sys.stderr)
            return 1
        try:
            sid = session_create.new(
                backend=backend, create=bool(kw.get("create")),
                attach=kw.get("attach"), name=kw.get("name"),
            )
        except ValueError as e:
            print(str(e), file=sys.stderr)
            return 1
        print(sid)  # token-frugal: bare id
        return 0

    if sub == "end":
        from .errors import NoSession
        from .session_ctx import resolve_session
        try:
            rec = resolve_session(kw.get("session"))
        except NoSession as e:
            print(str(e), file=sys.stderr)
            return e.exit_code
        print(session_create.end(rec))
        return 0

    if sub == "list":
        rows = reg.list_all()
        if kw.get("json"):
            sys.stdout.write(json.dumps(rows, indent=2, default=str) + "\n")
            return 0
        if not rows:
            print("(no sessions)")
            return 0
        for r in rows:
            print(f"  {r['id']:>3}  {r['backend']:9s} {r['owner']:6s} "
                  f"{(r.get('name') or '-'):16s} {r['daemon_endpoint']}")
        return 0

    if sub == "prune":
        idle = kw.get("idle", 3600)
        pruned = session_create.reap(idle_seconds=float(idle))
        print(f"pruned {len(pruned)} idle session(s).")
        return 0

    print(f"unknown session subcommand: {sub}", file=sys.stderr)
    return 1


def _cmd_whoami(args: list[str]) -> int:
    """``browser-skill whoami --session=ID`` — the ledger view of a session.

    Live-browser fields (group/tab count/sample URL) are filled by a daemon
    round-trip in Phase 5/6; for now we print only ledger-known fields.
    """
    from .errors import NoSession
    from .session_ctx import resolve_session

    kw = _parse_kv_args(args)
    try:
        rec = resolve_session(kw.get("session"))
    except NoSession as e:
        print(str(e), file=sys.stderr)
        return e.exit_code
    view = {k: rec.get(k) for k in ("id", "backend", "owner", "name", "daemon_endpoint")}
    sys.stdout.write(json.dumps(view, default=str) + "\n")
    return 0


def main(argv: Optional[list[str]] = None) -> None:
    argv = list(sys.argv[1:] if argv is None else argv)

    # `browser-skill` with stdin (heredoc) → inline.
    if not argv and not sys.stdin.isatty():
        from .repl import inline
        sys.exit(inline.run(sys.stdin))

    if not argv or argv[0] in {"-h", "--help"}:
        sys.stdout.write(HELP)
        sys.exit(0 if argv else 1)

    cmd = argv[0]
    rest = argv[1:]

    if cmd in {"--version", "version"}:
        print(__version__)
        sys.exit(0)
    if cmd == "task":
        sys.exit(_cmd_task(rest))
    if cmd == "doctor":
        sys.exit(_cmd_doctor(rest))
    if cmd == "install":
        sys.exit(_cmd_install(rest))
    if cmd == "list-tasks":
        sys.exit(_cmd_list_tasks(rest))
    if cmd == "save":
        sys.exit(_cmd_save(rest))
    if cmd == "solidify":
        # REVIEW.md F-16: design / README narrative uses "solidify";
        # the verb is the same as `save`. Alias keeps both spellings
        # working — agent muscle memory is forgiving.
        sys.exit(_cmd_save(rest))
    if cmd == "index":
        sys.exit(_cmd_index(rest))
    if cmd == "memory":
        sys.exit(_cmd_memory(rest))
    if cmd == "selftest":
        sys.exit(_cmd_selftest(rest))
    if cmd == "sub":
        sys.exit(_cmd_sub(rest))
    if cmd == "session":
        sys.exit(_cmd_session(rest))
    if cmd == "whoami":
        sys.exit(_cmd_whoami(rest))

    # Catch heredoc usage: `cat foo.py | browser-skill`.
    print(f"unknown command: {cmd!r}", file=sys.stderr)
    print(HELP, file=sys.stderr)
    sys.exit(1)
