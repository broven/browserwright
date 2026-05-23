"""``browserwright <<'PY' ... PY`` — single-shot heredoc execution.

The snippet is always exec'd **in-process**. The daemon (extension / rdp /
cloud) is reached on demand via the session client resolved from the current
``BD_SESSION``. The old cross-process Skill REPL daemon was removed (P3): it
froze ``BD_NAME``/backend into a shared singleton and forwarded heredocs
without their env — the documented cross-talk accident.
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


def run(stdin: IO[str]) -> int:
    """Execute heredoc input. Returns the desired exit code."""
    code = stdin.read()
    if not code.strip():
        print("usage: browserwright <<'PY'\\n  print(page_info())\\nPY",
              file=sys.stderr)
        return 1

    # P1: refuse loudly at the entrypoint when no session is in scope, rather
    # than silently sharing a browser via the import-time default.
    from ..errors import NoSession
    from ..session_ctx import resolve_session
    try:
        resolve_session()
    except NoSession as e:
        print(str(e), file=sys.stderr)
        return e.exit_code

    # Run in-process. Capture stdout so we can also record it in the session
    # history (which propose_solidify reads).
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
