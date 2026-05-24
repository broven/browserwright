"""Top-level ``browserwright`` CLI dispatch.

Subcommands:
  session new | end | list | prune        (P2: explicit session creation)
  whoami --session=ID
  task <site>/<name> [--arg=val ...]    NOT IN v0.1 ENTRY: minimal stub
  install
  doctor
  list-tasks [--site SITE]
  index rebuild
  memory show [--site SITE | --global]
  version
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Optional

from . import __version__


HELP = """browserwright — Layer 2 of the browser stack.

Usage:
  browserwright <<'PY'
  print(page_info())
  PY

  browserwright session new --backend=<extension|rdp> --name=NAME [--create | --attach=PORT]
  browserwright session end --session=ID
  browserwright session list [--json]
  browserwright session prune [--idle=SECONDS]
  browserwright whoami --session=ID
  browserwright userscript {push|list|remove|toggle|logs} ...

  browserwright task <site>/<name> [--key=value ...] [--isolated]
  browserwright list-tasks [--site SITE] [--query Q] [--json]

  browserwright sub add <git-url> [--name NAME]
  browserwright sub list [--json]
  browserwright sub update [--name NAME]
  browserwright sub remove --name NAME
  browserwright release {install-local|status|list|activate} ...

  browserwright install
  browserwright doctor [--json]
  browserwright index rebuild
  browserwright memory show [--site SITE | --global]
  browserwright memory forget --pattern PAT (--site SITE | --global) [--yes]
  browserwright memory replace --pattern PAT --with 'TEXT' (--site SITE | --global) [--yes]

  browserwright version [--json | check]
  browserwright --print-skill            (alias: print-skill)
"""


def _coerce(value: str) -> object:
    # try JSON first so callers can pass numbers/lists/etc.
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return value


def _parse_kv_args(args: list[str]) -> dict:
    out: dict[str, object] = {}
    i, n = 0, len(args)
    while i < n:
        a = args[i]
        if not a.startswith("--"):
            i += 1
            continue
        key, eq, value = a[2:].partition("=")
        if eq:
            out[key] = _coerce(value)
        elif i + 1 < n and not args[i + 1].startswith("--"):
            # space form: `--query "hacker news"` (the form --help advertises).
            # Consume the next token as the value unless it's another flag.
            out[key] = _coerce(args[i + 1])
            i += 1
        else:
            out[key] = True
        i += 1
    return out


def _cmd_task(args: list[str]) -> int:
    if not args:
        print("usage: browserwright task <site>/<name> [--key=val ...]", file=sys.stderr)
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
    """A4: a ``{status, message, fix}`` check table.

    ``doctor --json`` emits the machine form; default prints human-readable.
    Every ``fail`` check carries a non-empty ``fix`` (enforced in
    ``doctor_checks``). Exits nonzero (CI-style) if any check fails.
    """
    from .health import doctor_checks

    info = doctor_checks()
    info["skill_version"] = __version__
    checks = info.get("checks", [])
    any_fail = any(c.get("status") == "fail" for c in checks)

    if "--json" in args:
        sys.stdout.write(json.dumps(info, indent=2, default=str) + "\n")
        return 1 if any_fail else 0

    print(f"browserwright {__version__}")
    print()
    glyph = {"pass": "✓", "warn": "!", "fail": "✗"}
    for c in checks:
        status = c.get("status", "?")
        mark = glyph.get(status, "?")
        name = c.get("name", "?")
        print(f"  {mark} {name:14s} {c.get('message', '')}")
        # Always surface the recovery action for non-pass checks so the
        # human/agent reading the output has a next step.
        if status != "pass" and c.get("fix"):
            print(f"     fix: {c['fix']}")
    print()
    if any_fail:
        print("doctor: FAIL — address the fixes above.")
        return 1
    print("doctor: ok")
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


def _cmd_index(args: list[str]) -> int:
    if args and args[0] == "rebuild":
        from .discovery import rebuild_index
        out = rebuild_index()
        sys.stdout.write(json.dumps({"sites": len(out.get("sites", []))}, default=str) + "\n")
        return 0
    print("usage: browserwright index rebuild", file=sys.stderr)
    return 1


def _cmd_sub(args: list[str]) -> int:
    """``browserwright sub {add|list|update|remove} ...``."""
    if not args:
        print("usage: browserwright sub {add|list|update|remove} ...", file=sys.stderr)
        return 1
    sub = args[0]
    rest = args[1:]
    from . import subscriptions

    if sub == "add":
        if not rest or rest[0].startswith("--"):
            print("usage: browserwright sub add <git-url> [--name NAME]", file=sys.stderr)
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
            print("usage: browserwright sub remove --name NAME", file=sys.stderr)
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


def _cmd_memory(args: list[str]) -> int:
    if not args:
        print("usage: browserwright memory {show|forget|replace} ...", file=sys.stderr)
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
    """``browserwright session {new|end|list|prune} ...`` (P2)."""
    from . import session_create
    from . import session_registry as reg

    if not args:
        print("usage: browserwright session {new|end|list|prune} ...", file=sys.stderr)
        return 1
    sub = args[0]
    kw = _parse_kv_args(args[1:])

    if sub == "new":
        backend = kw.get("backend")
        if backend not in ("extension", "rdp"):
            print("usage: browserwright session new --backend=<extension|rdp> "
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
                  f"{(r.get('name') or '-'):16s}")
        return 0

    if sub == "prune":
        idle = kw.get("idle", 3600)
        pruned = session_create.reap(idle_seconds=float(idle))
        print(f"pruned {len(pruned)} idle session(s).")
        return 0

    print(f"unknown session subcommand: {sub}", file=sys.stderr)
    return 1


def _cmd_userscript(args: list[str]) -> int:
    # ``--verify`` is a browserwright-level convenience on ``push``: after a
    # successful push, reload the live tab and screenshot it so the agent sees
    # the effect in one step instead of the manual push→reload→screenshot
    # ritual. It is NOT a daemon flag, so strip it before delegating.
    verify = "--verify" in args
    fwd = [a for a in args if a != "--verify"]

    result = subprocess.run(["browserwright-daemon", "userscript", *fwd])
    if result.returncode != 0:
        # Push failed — don't reload/screenshot a stale state. Surface the
        # push failure so the agent fixes the script first.
        return result.returncode

    if verify and fwd and fwd[0] in ("push", "install"):
        # Push succeeded; reload the live tab (reload() waits for load) and
        # screenshot so the agent sees the effect in one step. This is a
        # convenience: if there's no drivable session/tab (e.g. run outside a
        # session), report that the push still SUCCEEDED rather than letting an
        # opaque reload error look like a push failure.
        try:
            from .api import capture_screenshot, reload

            reload()
            print(capture_screenshot())
        except Exception as e:
            print(f"pushed OK — --verify skipped (no drivable tab): {e}",
                  file=sys.stderr)

    return result.returncode


def _cmd_whoami(args: list[str]) -> int:
    """``browserwright whoami --session=ID`` — the ledger view of a session.

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
    view = {k: rec.get(k) for k in ("id", "backend", "owner", "name")}
    sys.stdout.write(json.dumps(view, default=str) + "\n")
    return 0


def _cmd_print_skill(_: list[str]) -> int:
    """D1: emit the agent-facing skill doc assembled from the running code.

    The version stamp and primitive surface are generated at runtime from
    ``browserwright.__version__`` and ``browserwright.EXPORTS`` respectively,
    so the printed instructions can never silently drift from the installed
    binary.
    """
    from . import skill_doc

    sys.stdout.write(skill_doc.render())
    return 0


def _cmd_version(args: list[str]) -> int:
    from .version import version_info

    info = version_info()
    if args and args[0] == "check":
        if "--json" in args:
            sys.stdout.write(json.dumps(info, sort_keys=True) + "\n")
        elif info["ok"]:
            print(f"browserwright {info['version']} (versions ok)")
        else:
            for issue in info["issues"]:
                print(f"{issue['code']}: {issue['message']}", file=sys.stderr)
        return 0 if info["ok"] else 1
    if args and args[0] == "--json":
        sys.stdout.write(json.dumps(info, sort_keys=True) + "\n")
        return 0
    print(__version__)
    return 0


def _cmd_release(args: list[str]) -> int:
    if not args:
        print(
            "usage: browserwright release {install-local|status|list|activate} ...",
            file=sys.stderr,
        )
        return 1
    from . import release_install

    sub = args[0]
    rest = args[1:]
    kw = _parse_kv_args(rest)
    try:
        if sub == "install-local":
            info = release_install.install_local(
                force=bool(kw.get("force")),
                activate_release=not bool(kw.get("no-activate")),
            )
            if kw.get("restart-daemon") and info["actions"].get("restart_daemon"):
                subprocess.run(["browserwright-daemon", "restart"], check=False)
            if kw.get("json"):
                sys.stdout.write(json.dumps(info, indent=2, sort_keys=True) + "\n")
            else:
                print(f"installed browserwright {info['version']} -> {info['path']}")
                if info.get("activated"):
                    print("activated release symlinks")
                if info["actions"].get("restart_daemon"):
                    print("next: restart daemon (`browserwright-daemon restart`)")
                if info["actions"].get("reload_chrome_extension"):
                    print(
                        "next: reload Chrome unpacked extension from "
                        f"{info.get('chrome_extension_sync', {}).get('path') or info['path'] + '/chrome-extension'}"
                    )
            return 0

        if sub == "status":
            info = release_install.status()
            if kw.get("json"):
                sys.stdout.write(json.dumps(info, indent=2, sort_keys=True) + "\n")
                return 0
            version = info.get("installed_version") or "(none)"
            print(f"installed release: {version}")
            daemon = info.get("daemon") or {}
            daemon_version = daemon.get("version") or "(not running)"
            suffix = "  restart required" if daemon.get("restart_required") else ""
            print(f"running daemon:    {daemon_version}{suffix}")
            ok = all(row.get("ok") for row in info.get("skill", []))
            print(f"skill install:     {'release-linked ok' if ok else 'needs relink'}")
            return 0

        if sub == "list":
            rows = release_install.list_releases()
            if kw.get("json"):
                sys.stdout.write(json.dumps(rows, indent=2, sort_keys=True) + "\n")
                return 0
            if not rows:
                print("(no releases installed)")
                return 0
            for row in rows:
                mark = "*" if row.get("active") else " "
                print(f"{mark} {row.get('version')}  {row.get('path')}")
            return 0

        if sub == "activate":
            if not rest or rest[0].startswith("--"):
                print("usage: browserwright release activate <version>", file=sys.stderr)
                return 1
            info = release_install.activate(rest[0])
            if kw.get("json"):
                sys.stdout.write(json.dumps(info, indent=2, sort_keys=True) + "\n")
            else:
                print(f"activated browserwright {info['version']} -> {info['path']}")
                print("next: restart daemon and reload Chrome extension if that release differs")
            return 0
    except release_install.ReleaseError as e:
        print(f"release error: {e}", file=sys.stderr)
        return 1

    print(f"unknown release subcommand: {sub}", file=sys.stderr)
    return 1


def main(argv: Optional[list[str]] = None) -> None:
    argv = list(sys.argv[1:] if argv is None else argv)

    # `browserwright` with stdin (heredoc) → inline.
    if not argv and not sys.stdin.isatty():
        from .repl import inline
        sys.exit(inline.run(sys.stdin))

    # `--print-skill` is a flag (leading dash) but is a real command, not help;
    # intercept it before the help check below.
    if argv and argv[0] in {"--print-skill", "print-skill"}:
        sys.exit(_cmd_print_skill(argv[1:]))

    if not argv or argv[0] in {"-h", "--help"}:
        sys.stdout.write(HELP)
        sys.exit(0 if argv else 1)

    cmd = argv[0]
    rest = argv[1:]

    if cmd in {"--version", "version"}:
        sys.exit(_cmd_version(rest))
    if cmd == "task":
        sys.exit(_cmd_task(rest))
    if cmd == "doctor":
        sys.exit(_cmd_doctor(rest))
    if cmd == "install":
        sys.exit(_cmd_install(rest))
    if cmd == "list-tasks":
        sys.exit(_cmd_list_tasks(rest))
    if cmd == "index":
        sys.exit(_cmd_index(rest))
    if cmd == "memory":
        sys.exit(_cmd_memory(rest))
    if cmd == "sub":
        sys.exit(_cmd_sub(rest))
    if cmd == "release":
        sys.exit(_cmd_release(rest))
    if cmd == "session":
        sys.exit(_cmd_session(rest))
    if cmd == "whoami":
        sys.exit(_cmd_whoami(rest))
    if cmd == "userscript":
        sys.exit(_cmd_userscript(rest))

    # Catch heredoc usage: `cat foo.py | browserwright`.
    print(f"unknown command: {cmd!r}", file=sys.stderr)
    print(HELP, file=sys.stderr)
    sys.exit(1)
