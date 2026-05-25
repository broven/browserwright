"""``browserwright <<'PY' ... PY`` — single-shot heredoc execution.

Two paths, chosen by a cheap static pre-check on the code (Phase B, Fork 7):

  - **In-process** (Phase C path, unchanged): a heredoc that touches NONE of
    ``{page, context, snapshot, state, reset}`` — pure ``memory()`` /
    site-skill / ``http_get`` — is exec'd here, in this short-lived process. It
    never spawns or contacts an executor (stays lightweight).
  - **Shipped to the executor** (Phase B path): a heredoc that references any of
    those names is shipped WHOLE to the session's resident, per-session executor
    subprocess, where ``page`` / ``context`` are LIVE objects that survive
    across heredoc calls and ``state`` is a persistent dict. You cannot return a
    live cross-process ``Page`` into a local ``exec``, so the entire body runs
    there.

The old cross-process Skill REPL daemon was removed (P3): it froze
``BD_NAME``/backend into a shared SINGLETON and forwarded heredocs without their
env — the documented cross-talk accident. Phase B's executor avoids that by
keying strictly on ``session_id`` (playwriter's ``Map<sessionId, executor>``),
never a shared singleton.
"""
from __future__ import annotations

import io
import json
import sys
import traceback
from contextlib import redirect_stdout
from typing import IO

from ..errors import BrowserwrightError, serialize
from . import _namespace

# Names whose presence routes the heredoc to the persistent executor (Fork 7).
# `state` / `reset` are executor-only (live across calls / acts on live
# objects); `page` / `context` / `snapshot` are live cross-process objects.
_EXECUTOR_NAMES = frozenset({"page", "context", "snapshot", "state", "reset"})


def _touches_executor_surface(code_obj) -> bool:
    """True iff the compiled code references any executor-only name.

    Uses ``co_names`` (free/global name references) — cheap and deterministic.
    KNOWN ESCAPE (acceptable): indirect access like ``g = globals(); g['page']``
    evades ``co_names`` — but the fallback (in-process) is only WRONG in that it
    can't actually serve a live ``page``; such code would raise a plain
    ``NameError`` exactly as it does today, never silently misbehave. The common
    case (writing ``page``/``state`` directly) routes correctly. We also scan
    nested code objects (functions/comprehensions) so a name used only inside a
    ``def`` still routes to the executor."""
    seen = [code_obj]
    while seen:
        co = seen.pop()
        if _EXECUTOR_NAMES.intersection(co.co_names):
            return True
        for const in co.co_consts:
            if hasattr(const, "co_names"):  # nested code object
                seen.append(const)
    return False


def run(stdin: IO[str]) -> int:
    """Execute heredoc input. Returns the desired exit code."""
    code = stdin.read()
    if not code.strip():
        print("usage: browserwright <<'PY'\\n  print(snapshot())\\nPY",
              file=sys.stderr)
        return 1

    # P1: refuse loudly at the entrypoint when no session is in scope, then
    # bind that explicit ledger record so primitives drive the session's
    # daemon/backend — never an env-guessed default.
    from ..errors import NoSession
    from ..session import Session, set_session
    from ..session_ctx import resolve_session
    try:
        rec = resolve_session()
    except NoSession as e:
        print(str(e), file=sys.stderr)
        return e.exit_code
    sess = Session(record=rec)
    set_session(sess)

    # Static pre-check: ship to the executor iff the code touches the live
    # browser surface; otherwise run the lightweight in-process path.
    try:
        code_obj = compile(code, "<inline>", "exec")
    except SyntaxError:
        # Let the in-process path raise the SyntaxError with a full traceback
        # (identical behaviour to before); never ship un-compilable code.
        code_obj = None
    if code_obj is not None and _touches_executor_surface(code_obj):
        return _run_on_executor(sess, code)

    # Run in-process. Capture stdout so we can replay it after the exec.
    globals_ = _namespace.build_globals()
    buf = io.StringIO()
    try:
        with redirect_stdout(buf):
            exec(code_obj if code_obj is not None
                 else compile(code, "<inline>", "exec"), globals_)
    except BrowserwrightError as e:
        sys.stdout.write(buf.getvalue())
        sys.stderr.write(json.dumps(serialize(e)) + "\n")
        return e.exit_code
    except SystemExit as e:
        sys.stdout.write(buf.getvalue())
        return int(e.code) if isinstance(e.code, int) else 0
    except Exception:  # noqa: BLE001
        sys.stdout.write(buf.getvalue())
        sys.stderr.write(traceback.format_exc())
        return 3
    finally:
        # Phase C: tear down the lazy Playwright connection at heredoc end. A
        # no-op when `page`/`context` were never accessed (nothing connected).
        # close() disconnects the CDP transport only — it never closes the
        # user's real tabs/browser. Fully suppressed so cleanup can't change
        # the heredoc's exit code.
        handle = globals_.get("__bw_playwright_handle__")
        if handle is not None:
            try:
                handle.close()
            except Exception:  # noqa: BLE001
                pass
    sys.stdout.write(buf.getvalue())
    return 0


def _run_on_executor(sess, code: str) -> int:
    """Ship the whole code body to the session's persistent executor and replay
    its response locally (Phase B path).

    The executor holds live ``page`` / ``context`` + persistent ``state`` across
    heredoc calls. We print its captured console to stdout, surface any error to
    stderr in the same shape the in-process path uses (``serialize`` dict for a
    BrowserwrightError, else the raw type/msg), and propagate its exit code. The
    in-process ``finally`` teardown does NOT run on this path — no local
    ``__bw_playwright_handle__`` was built; the executor owns teardown."""
    from .._executor.client import run_on_executor

    try:
        # ExecutorUnavailable is a BrowserwrightError subclass — caught below.
        resp = run_on_executor(sess, code)
    except BrowserwrightError as e:
        sys.stderr.write(json.dumps(serialize(e)) + "\n")
        return e.exit_code
    except Exception:  # noqa: BLE001 - never let transport blow up opaque
        sys.stderr.write(traceback.format_exc())
        return 3

    if resp.console:
        sys.stdout.write(resp.console)
    for w in resp.warnings:
        sys.stderr.write(f"[WARNING] {w}\n")
    for shot in resp.screenshots:
        path = shot.get("path") if isinstance(shot, dict) else None
        if path:
            sys.stderr.write(f"[screenshot] {path}\n")
    if resp.return_value is not None:
        sys.stdout.write(f"[return value] {resp.return_value}\n")
    if resp.truncated:
        sys.stderr.write("[output truncated]\n")
    if resp.error is not None:
        tb = resp.error.get("tb") or resp.error.get("traceback") \
            if isinstance(resp.error, dict) else None
        if tb:
            # Mirror the in-process path: a generic exception writes its full
            # traceback to stderr (the serialized envelope carries it).
            sys.stderr.write(tb if tb.endswith("\n") else tb + "\n")
        else:
            sys.stderr.write(json.dumps(resp.error) + "\n")
    return resp.exit_code
