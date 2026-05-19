"""``browser-skill <<'PY' ... PY`` — single-shot heredoc execution.

Dispatch order:

  1. If a long-lived Skill REPL daemon is already running
     (`/tmp/browser-skill.sock`) → send the snippet over it. Single
     shared ws across calls, no per-call connection cost.

  2. Otherwise exec the snippet in-process. The daemon (extension /
     rdp / cloud) is reached on demand via the standard session client.
"""
from __future__ import annotations

import io
import json
import sys
import traceback
from contextlib import redirect_stdout
from typing import IO

from ..errors import BrowserSkillError, serialize
from ..session import current_session
from . import _namespace
from .client import is_repl_running, send_exec


def run(stdin: IO[str]) -> int:
    """Execute heredoc input. Returns the desired exit code."""
    code = stdin.read()
    if not code.strip():
        print("usage: browser-skill <<'PY'\\n  print(page_info())\\nPY",
              file=sys.stderr)
        return 1

    # (1) re-use the Skill REPL daemon when present — single shared ws.
    if is_repl_running():
        try:
            reply = send_exec(code)
        except Exception as e:  # noqa: BLE001
            print(f"repl daemon error: {e}", file=sys.stderr)
            return 2
        if reply.get("stdout"):
            sys.stdout.write(reply["stdout"])
        if reply.get("stderr"):
            sys.stderr.write(reply["stderr"])
        if reply.get("exception"):
            sys.stderr.write(json.dumps(reply["exception"]) + "\n")
            return _exit_code_for(reply["exception"].get("type"))
        return 0

    # (2) Otherwise run in-process. Capture stdout so we can also record it in
    # the session history (which propose_solidify reads).
    globals_ = _namespace.build_globals()
    buf = io.StringIO()
    try:
        with redirect_stdout(buf):
            exec(compile(code, "<inline>", "exec"), globals_)
    except BrowserSkillError as e:
        sys.stdout.write(buf.getvalue())
        sys.stderr.write(json.dumps(serialize(e)) + "\n")
        current_session().record(code, ok=False, stdout=buf.getvalue(),
                                 exception=type(e).__name__)
        return e.exit_code
    except SystemExit as e:
        sys.stdout.write(buf.getvalue())
        return int(e.code) if isinstance(e.code, int) else 0
    except Exception as e:  # noqa: BLE001
        sys.stdout.write(buf.getvalue())
        sys.stderr.write(traceback.format_exc())
        current_session().record(code, ok=False, stdout=buf.getvalue(),
                                 exception=type(e).__name__)
        return 3
    sys.stdout.write(buf.getvalue())
    current_session().record(code, ok=True, stdout=buf.getvalue())
    return 0


def _exit_code_for(type_name: str | None) -> int:
    return {
        "AuthWall": 4,
        "Captcha": 5,
        "DaemonUnavailable": 2,
        "NeedsUserConfirm": 1,
    }.get(type_name or "", 3)
