"""Top-level ``browserwright`` CLI dispatch.

Subcommands:
  session new | reset | end | list | prune        (P2: explicit session creation)
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
  browserwright -s <session-id> [--env NAME ...] -e 'page.goto("https://example.com"); print(page.title())'
  browserwright -s <session-id> [--env NAME ...] -f script.py
  browserwright -s <session-id> [--env NAME ...] --code-stdin < script.py

  browserwright session new --backend=<extension|rdp|env> --name=SESSION_LABEL [--create | --attach=PORT]
  browserwright session reset <id>
  browserwright session end --session=ID
  browserwright session list [--json]
  browserwright session prune [--idle=SECONDS]
  browserwright whoami --session=ID
  browserwright userscript {push|list|remove|toggle|logs} ...

  browserwright -s <session-id> task <site>/<name> [--key=value ...] [--isolated]
  browserwright list-tasks [--site SITE] [--query Q] [--json]

  browserwright install
  browserwright doctor [--json]
  browserwright index rebuild
  browserwright memory show [--site SITE | --global]
  browserwright memory forget --pattern PAT (--site SITE | --global) [--yes]
  browserwright memory replace --pattern PAT --with 'TEXT' (--site SITE | --global) [--yes]

  browserwright version [--json | check]
  browserwright --print-skill            (alias: print-skill)
"""

TASK_HELP = """Usage:
  browserwright -s <session-id> task <site>/<name> [--key=value ...] [--isolated]

Runs a site-skill task in the bound Browserwright session. Session may also be
provided as --session=<id> after `task`, or via BD_SESSION.

Flags:
  --json-args JSON       merge a JSON object into task args
  --json-output          print task result as JSON
  --output json          alias for --json-output
"""

USERSCRIPT_HELP = """Usage:
  browserwright [-s <session-id>] userscript {push|install|list|remove|toggle|logs} ...

For push/install:
  browserwright -s <session-id> userscript push ./script.user.js [--verify]

`--verify` is handled by browserwright: after a successful daemon push it
reloads the bound tab and prints a screenshot path. Session may also come from
--session=<id> after `userscript`, or BD_SESSION.
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


def _split_global_session(args: list[str]) -> tuple[Optional[str], list[str], Optional[str]]:
    """Extract a leading global ``-s/--session`` without changing command args."""
    session_id: Optional[str] = None
    rest: list[str] = []
    i, n = 0, len(args)
    while i < n:
        a = args[i]
        if a in {"-s", "--session"}:
            if i + 1 >= n:
                return None, [], f"{a} requires a value"
            session_id = args[i + 1]
            i += 2
            continue
        if a.startswith("--session="):
            session_id = a.split("=", 1)[1]
            i += 1
            continue
        rest.extend(args[i:])
        break
    return session_id, rest, None


def _extract_session_arg(args: list[str]) -> tuple[Optional[str], list[str], Optional[str]]:
    """Remove ``-s/--session`` from a subcommand arg list."""
    session_id: Optional[str] = None
    out: list[str] = []
    i, n = 0, len(args)
    while i < n:
        a = args[i]
        if a in {"-s", "--session"}:
            if i + 1 >= n:
                return None, [], f"{a} requires a value"
            session_id = args[i + 1]
            i += 2
            continue
        if a.startswith("--session="):
            session_id = a.split("=", 1)[1]
            i += 1
            continue
        out.append(a)
        i += 1
    return session_id, out, None


def _bind_cli_session(session_id: Optional[str]):
    from .errors import NoSession
    from .session import Session, set_session
    from .session_ctx import resolve_session_or_env

    try:
        rec = resolve_session_or_env(session_id)
    except NoSession as e:
        print(str(e), file=sys.stderr)
        return e.exit_code
    set_session(Session(record=rec))
    return 0


def _parse_execute_args(
    args: list[str],
) -> tuple[Optional[str], Optional[str], list[str], Optional[str]]:
    """Parse playwriter-style execution flags.

    Supports both short and long forms:
      browserwright -s 1 -e 'print(snapshot())'
      browserwright --session=1 --execute='print(snapshot())'
    """
    session_id: Optional[str] = None
    code: Optional[str] = None
    code_file: Optional[str] = None
    env_names: list[str] = []
    code_stdin = False
    i, n = 0, len(args)
    while i < n:
        a = args[i]
        if a in {"-s", "--session"}:
            if i + 1 >= n:
                return None, None, [], f"{a} requires a value"
            session_id = args[i + 1]
            i += 2
            continue
        if a.startswith("--session="):
            session_id = a.split("=", 1)[1]
            i += 1
            continue
        if a == "--env":
            if i + 1 >= n or args[i + 1].startswith("-"):
                return None, None, [], "--env requires a variable name"
            name = args[i + 1]
            error = _validate_execute_env_name(name)
            if error:
                return None, None, [], error
            env_names.append(name)
            i += 2
            continue
        if a.startswith("--env="):
            name = a.split("=", 1)[1]
            error = _validate_execute_env_name(name)
            if error:
                return None, None, [], error
            env_names.append(name)
            i += 1
            continue
        if a in {"-e", "--execute"}:
            if i + 1 >= n:
                return None, None, [], f"{a} requires a value"
            if code is not None or code_file is not None or code_stdin:
                return (
                    None, None, [],
                    "pass only one of -e, -f, or --code-stdin",
                )
            value = args[i + 1]
            if value == "-":
                code_stdin = True
            else:
                code = value
            i += 2
            continue
        if a.startswith("--execute="):
            if code is not None or code_file is not None or code_stdin:
                return (
                    None, None, [],
                    "pass only one of -e, -f, or --code-stdin",
                )
            value = a.split("=", 1)[1]
            if value == "-":
                code_stdin = True
            else:
                code = value
            i += 1
            continue
        if a in {"-f", "--code-file"}:
            if i + 1 >= n:
                return None, None, [], f"{a} requires a value"
            if code is not None or code_file is not None or code_stdin:
                return (
                    None, None, [],
                    "pass only one of -e, -f, or --code-stdin",
                )
            code_file = args[i + 1]
            i += 2
            continue
        if a.startswith("--code-file="):
            if code is not None or code_file is not None or code_stdin:
                return (
                    None, None, [],
                    "pass only one of -e, -f, or --code-stdin",
                )
            code_file = a.split("=", 1)[1]
            i += 1
            continue
        if a == "--code-stdin":
            if code is not None or code_file is not None or code_stdin:
                return (
                    None, None, [],
                    "pass only one of -e, -f, or --code-stdin",
                )
            code_stdin = True
            i += 1
            continue
        return None, None, [], f"unknown execute argument: {a!r}"
    if not session_id:
        return None, None, [], "missing session id: pass -s <id>"
    if code_file is not None:
        try:
            code = Path(code_file).read_text()
        except OSError as e:
            return (
                None, None, [],
                f"cannot read code file {code_file!r}: {e}",
            )
    elif code_stdin:
        code = sys.stdin.read()
    if code is None:
        return (
            None, None, [],
            "missing code: pass -e '<python>', -f <path>, or --code-stdin",
        )
    return session_id, code, env_names, None


def _validate_execute_env_name(name: str) -> Optional[str]:
    """Validate one name-only ``--env`` argument without echoing its value."""
    from ._executor.protocol import is_valid_env_name

    if not name:
        return "--env requires a variable name"
    if "=" in name:
        var_name = name.split("=", 1)[0]
        if is_valid_env_name(var_name):
            return (
                f"--env {var_name!r} must not include a value; "
                "pass the variable name only"
            )
        return "--env must contain a variable name only, not NAME=value"
    if not is_valid_env_name(name):
        return "invalid environment variable name for --env"
    return None


def _resolve_execute_env(
    env_names: list[str],
) -> tuple[dict[str, str], Optional[str]]:
    """Select explicit variables from this CLI process for one request."""
    values: dict[str, str] = {}
    for name in env_names:
        if name not in os.environ:
            return {}, f"environment variable {name!r} is not set"
        values[name] = os.environ[name]
    return values, None


def _cmd_execute(args: list[str]) -> int:
    session_id, code, env_names, err = _parse_execute_args(args)
    if err:
        print(f"usage error: {err}", file=sys.stderr)
        print("usage: browserwright -s <session-id> [--env NAME ...] "
              "(-e 'print(snapshot())' | -f script.py | --code-stdin)",
              file=sys.stderr)
        return 1
    request_env, err = _resolve_execute_env(env_names)
    if err:
        print(f"usage error: {err}", file=sys.stderr)
        return 1
    from .repl import inline
    return inline.run_code(
        code or "",
        session_id=session_id or "",
        env=request_env,
    )


def _cmd_task(args: list[str], *, session_id: Optional[str] = None) -> int:
    if args and args[0] in {"-h", "--help"}:
        sys.stdout.write(TASK_HELP)
        return 0
    if not args:
        print("usage: browserwright -s <session-id> task <site>/<name> [--key=val ...]", file=sys.stderr)
        return 1
    inner_session, args, err = _extract_session_arg(args)
    if err:
        print(f"usage error: {err}", file=sys.stderr)
        return 1
    if inner_session:
        session_id = inner_session
    if not args:
        print("usage: browserwright -s <session-id> task <site>/<name> [--key=val ...]", file=sys.stderr)
        return 1
    spec = args[0]
    if "/" not in spec:
        print("task spec must be <site>/<name>", file=sys.stderr)
        return 1
    bound = _bind_cli_session(session_id)
    if bound:
        return bound
    site, name = spec.split("/", 1)
    kwargs = _parse_kv_args(args[1:])
    if kwargs.get("output") == "json":
        kwargs["json_output"] = True
    json_output = bool(kwargs.pop("json_output", False) or kwargs.pop("json-output", False))
    kwargs.pop("output", None)
    # JSON-args envelope for Layer 3 callers.
    js = kwargs.pop("json-args", None)
    if js is not None:
        if isinstance(js, str):
            kwargs.update(json.loads(js))
        elif isinstance(js, dict):
            kwargs.update(js)
    isolated = bool(kwargs.pop("isolated", False))
    from ._executor.client import run_task_on_executor
    from .session import current_session

    try:
        response = run_task_on_executor(
            current_session(),
            site,
            name,
            args=kwargs,
            isolated=isolated,
        )
    except Exception as e:  # noqa: BLE001
        print(f"task crashed: {e!r}", file=sys.stderr)
        return 3
    if response.console:
        sys.stdout.write(response.console)
    for warning in response.warnings:
        sys.stderr.write(f"[WARNING] {warning}\n")
    if response.truncated:
        sys.stderr.write("[output truncated]\n")
    if response.error is not None:
        error_type = str(response.error.get("type") or "Exception")
        message = str(response.error.get("msg") or "")
        if error_type == "FileNotFoundError":
            print(f"task not found: {message}", file=sys.stderr)
            return 1
        print(f"task crashed: {error_type}({message!r})", file=sys.stderr)
        return response.exit_code or 3
    if response.task_result_json is None:
        print("task crashed: executor returned no task result", file=sys.stderr)
        return 3
    if json_output:
        result = json.loads(response.task_result_json)
        sys.stdout.write(json.dumps(result, default=str))
    else:
        if response.return_value is None:
            print("task crashed: executor returned no repr result", file=sys.stderr)
            return 3
        sys.stdout.write(response.return_value)
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

    if "--json" in args or "--output=json" in args or args[-2:] == ["--output", "json"]:
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
    if kwargs.get("json") or kwargs.get("output") == "json":
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


def _cmd_session(args: list[str], *, session_id: Optional[str] = None) -> int:
    """``browserwright session {new|reset|end|list|prune} ...`` (P2)."""
    from . import session_create
    from . import session_registry as reg

    if not args:
        print("usage: browserwright session {new|reset|end|list|prune} ...", file=sys.stderr)
        return 1
    inner_session, args, err = _extract_session_arg(args)
    if err:
        print(f"usage error: {err}", file=sys.stderr)
        return 1
    if inner_session:
        session_id = inner_session
    if not args:
        print("usage: browserwright session {new|reset|end|list|prune} ...", file=sys.stderr)
        return 1
    sub = args[0]
    kw = _parse_kv_args(args[1:])

    if sub == "new":
        backend = kw.get("backend")
        if backend not in ("extension", "rdp"):
            if backend == "env":
                # Name the replacement, not just the rejection: `env` was the
                # same real-CDP backend with the endpoint coming from a
                # process-global env var instead of the session (#38).
                print(session_create._unknown_backend_message("env"), file=sys.stderr)
                return 1
            print("usage: browserwright session new --backend=<extension|rdp> "
                  "--name=SESSION_LABEL [--create | --attach=<port|url>]",
                  file=sys.stderr)
            print("--name is a short task-specific session label. Extension sessions "
                  "use it as the Chrome tab group title; rdp sessions use it only to "
                  "label the browser session. --create launches an isolated browser "
                  "we own; --attach borrows one we don't — a local port (9222) or a "
                  "CDP URL (ws://…, or https://… for a cloud/anti-detect browser).",
                  file=sys.stderr)
            return 1
        try:
            sid = session_create.new(
                backend=backend, create=bool(kw.get("create")),
                attach=kw.get("attach"), name=kw.get("name"),
            )
        except ValueError as e:
            print(str(e), file=sys.stderr)
            return 1
        print(f"OK: session {sid} created", file=sys.stderr)
        print(sid)  # token-frugal: bare id
        return 0

    if sub == "end":
        from .errors import BrowserwrightError
        from .session_ctx import resolve_session_or_env
        try:
            rec = resolve_session_or_env(session_id)
            message = session_create.end(rec)
        except BrowserwrightError as e:
            print(str(e), file=sys.stderr)
            return e.exit_code
        print(message)
        return 0

    if sub == "reset":
        from .errors import BrowserwrightError
        from .session_ctx import resolve_session_or_env
        raw_sid = args[1] if len(args) > 1 and not args[1].startswith("--") else session_id
        try:
            rec = resolve_session_or_env(raw_sid)
            message = session_create.reset_executor(rec)
        except BrowserwrightError as e:
            print(str(e), file=sys.stderr)
            return e.exit_code
        print(message)
        return 0

    if sub == "list":
        rows = reg.list_all()
        if kw.get("json") or kw.get("output") == "json":
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
        idle = kw.get("idle", 24 * 3600)
        pruned = session_create.reap(idle_seconds=float(idle))
        print(f"pruned {len(pruned)} idle session(s).")
        return 0

    print(f"unknown session subcommand: {sub}", file=sys.stderr)
    return 1


def _fresh_screenshot_path() -> str:
    """A non-colliding /tmp png path for the userscript --verify screenshot."""
    i = 0
    while True:
        cand = Path("/tmp") / f"browserwright-shot-{os.getpid()}-{i}.png"
        if not cand.exists():
            return str(cand)
        i += 1


def _cmd_userscript(args: list[str], *, session_id: Optional[str] = None) -> int:
    if not args or args[0] in {"-h", "--help"}:
        sys.stdout.write(USERSCRIPT_HELP)
        return 0 if args else 1
    inner_session, args, err = _extract_session_arg(args)
    if err:
        print(f"usage error: {err}", file=sys.stderr)
        return 1
    if inner_session:
        session_id = inner_session
    # ``--verify`` is a browserwright-level convenience on ``push``: after a
    # successful push, reload the live tab and screenshot it so the agent sees
    # the effect in one step instead of the manual push→reload→screenshot
    # ritual. It is NOT a daemon flag, so strip it before delegating.
    verify = "--verify" in args
    fwd = [a for a in args if a != "--verify"]

    daemon_cmd = ["browserwright-daemon", "userscript"]
    if session_id:
        daemon_cmd += ["--session", session_id]
    result = subprocess.run([*daemon_cmd, *fwd])
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
            bound = _bind_cli_session(session_id)
            if bound:
                raise RuntimeError(
                    "no drivable session bound; pass -s <id> or set BD_SESSION"
                )
            # Verification is another instruction in this session, so route
            # it through the resident executor instead of creating a second
            # Playwright connection. This preserves FIFO ordering and the
            # exact page binding used by -e and task.
            from ._executor.client import run_on_executor
            from .session import current_session

            shot = _fresh_screenshot_path()
            response = run_on_executor(
                current_session(),
                "page.reload(wait_until='load')\n"
                f"page.screenshot(path={shot!r})",
            )
            if response.error is not None or response.exit_code != 0:
                error = response.error or {
                    "type": "ExecutorError",
                    "msg": f"verify exited with code {response.exit_code}",
                }
                raise RuntimeError(
                    f"{error.get('type', 'ExecutorError')}: "
                    f"{error.get('msg', '')}"
                )
            print(shot)
        except Exception as e:
            print(f"pushed OK — --verify skipped (no drivable tab): {e}",
                  file=sys.stderr)

    return result.returncode


def _cmd_whoami(args: list[str], *, session_id: Optional[str] = None) -> int:
    """``browserwright whoami --session=ID`` — the ledger view of a session.

    Live-browser fields (group/tab count/sample URL) are filled by a daemon
    round-trip in Phase 5/6; for now we print only ledger-known fields.
    """
    from .errors import NoSession
    from .session_ctx import resolve_session_or_env

    inner_session, args, err = _extract_session_arg(args)
    if err:
        print(f"usage error: {err}", file=sys.stderr)
        return 1
    if inner_session:
        session_id = inner_session
    kw = _parse_kv_args(args)
    try:
        rec = resolve_session_or_env(session_id or kw.get("session"))
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
        relay_status = _extension_relay_status()
        if relay_status:
            info["daemon_version"] = relay_status.get("daemon_version")
            info["running_extensions"] = relay_status.get("extension_details") or []
        if "--json" in args:
            sys.stdout.write(json.dumps(info, sort_keys=True) + "\n")
        elif info["ok"]:
            print(f"browserwright {info['version']} (versions ok)")
        else:
            for issue in info["issues"]:
                print(f"{issue['code']}: {issue['message']}", file=sys.stderr)
        if "--json" not in args:
            for ext in info.get("running_extensions") or []:
                print(
                    "extension "
                    f"{ext.get('install_id') or '?'} "
                    f"version={ext.get('browserwright_version') or ext.get('version') or '?'} "
                    f"daemon={ext.get('daemon_version') or '?'} "
                    f"drift={ext.get('version_drift') or '?'}"
                )
        return 0 if info["ok"] else 1
    if args and args[0] == "--json":
        sys.stdout.write(json.dumps(info, sort_keys=True) + "\n")
        return 0
    print(__version__)
    return 0


def _extension_relay_status() -> dict | None:
    import os
    import urllib.request

    port = os.environ.get("BD_EXTENSION_PORT") or "19989"
    try:
        with urllib.request.urlopen(
            f"http://127.0.0.1:{port}/__status__",
            timeout=1.0,
        ) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
        return payload if isinstance(payload, dict) else None
    except Exception:
        return None


def main(argv: Optional[list[str]] = None) -> None:
    argv = list(sys.argv[1:] if argv is None else argv)

    # `--print-skill` is a flag (leading dash) but is a real command, not help;
    # intercept it before the help check below.
    if argv and argv[0] in {"--print-skill", "print-skill"}:
        sys.exit(_cmd_print_skill(argv[1:]))

    if not argv and not sys.stdin.isatty():
        print("usage: browserwright -s <session-id> -e 'print(snapshot())'",
              file=sys.stderr)
        sys.exit(1)

    if not argv or argv[0] in {"-h", "--help"}:
        sys.stdout.write(HELP)
        sys.exit(0 if argv else 1)

    global_session, command_argv, session_err = _split_global_session(argv)
    if session_err:
        print(f"usage error: {session_err}", file=sys.stderr)
        sys.exit(1)
    if global_session is not None:
        if command_argv and (
            command_argv[0] in {"-e", "--execute", "-f", "--code-file", "--code-stdin"}
            or command_argv[0].startswith("--execute=")
            or command_argv[0].startswith("--code-file=")
            or command_argv[0] == "--env"
            or command_argv[0].startswith("--env=")
        ):
            sys.exit(_cmd_execute(argv))
        argv = command_argv
        if not argv:
            print("usage error: -s/--session requires a command or execute code", file=sys.stderr)
            sys.exit(1)

    cmd = argv[0]
    rest = argv[1:]

    if cmd in {"--version", "version"}:
        sys.exit(_cmd_version(rest))
    if cmd == "task":
        sys.exit(_cmd_task(rest, session_id=global_session))
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
    if cmd == "session":
        sys.exit(_cmd_session(rest, session_id=global_session))
    if cmd == "whoami":
        sys.exit(_cmd_whoami(rest, session_id=global_session))
    if cmd == "userscript":
        sys.exit(_cmd_userscript(rest, session_id=global_session))

    print(f"unknown command: {cmd!r}", file=sys.stderr)
    print(HELP, file=sys.stderr)
    sys.exit(1)
