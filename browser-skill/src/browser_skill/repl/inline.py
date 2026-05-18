"""``browser-skill <<'PY' ... PY`` — single-shot heredoc execution.

P0 #75 — Inline heredoc auto-abort on autoconnect (Chrome 144+ popup
accumulation defense, design.md §A.1 footnote).

Chrome 144+ accumulates "Allow remote debugging?" popups; past a threshold
the browser freezes (see chrome-popup-accumulation-bug). Daemon side has
its own rate-limiter (#74), but the **best** UX is to abort *before* we
even open a ws — that way the agent gets a clear, actionable error
without burning a popup or a daemon retry.

Decision tree for a heredoc invocation:

  1. If a long-lived Skill REPL daemon is already running
     (`/tmp/browser-skill.sock`) → dispatch the snippet over it. Single
     shared ws, no per-call popup → safe, always proceed.

  2. If a Mode B `browser-daemon serve` socket is alive → its upstream
     ws is shared too. Safe, always proceed.

  3. If the user explicitly opted in via ``BS_FORCE_AUTOCONNECT_INLINE=1``
     → proceed (escape hatch, e.g. one-off CI smoke test).

  4. If a direct ws URL is supplied (``BS_CDP_WS`` / ``BU_CDP_WS``) →
     proceed; daemon isn't involved at all.

  5. Otherwise ask the daemon's ``doctor`` what backend would be picked.
     If recommended is ``autoconnect`` OR its ``ux_cost`` mentions
     ``popup`` → **abort with exit 2** and explain the alternatives.

     - If the doctor call itself was rate-limited by the daemon (#74),
       surface the rate-limit message verbatim and abort with the same
       exit code — do NOT retry.

     - If doctor was simply unreachable (binary missing, transient
       failure) → proceed; we can't prove there's a popup risk.
"""
from __future__ import annotations

import io
import json
import os
import sys
import traceback
from contextlib import redirect_stdout
from typing import IO, Tuple

from .. import _hardening
from ..errors import BrowserSkillError, serialize
from ..session import current_session
from . import _namespace
from .client import is_repl_running, send_exec


_ABORT_EXIT = 2  # spec §A.1: "browser unavailable / daemon won't start"


def _abort_message(daemon_reason: str = "") -> str:
    reason_line = f"  daemon: {daemon_reason}\n" if daemon_reason else ""
    return (
        "Refusing inline heredoc on autoconnect path: each call triggers a "
        "Chrome 'Allow remote debugging?' popup, and Chrome 144+ accumulates "
        "these popups (may freeze Chrome).\n"
        f"{reason_line}"
        "Switch to one of:\n"
        "  * `browser-skill repl start`  (one popup, then reused for the session), OR\n"
        "  * `browser-daemon launch-chrome --port 9333 --profile /tmp/bs-isolated`\n"
        "    then `BD_PORT=9333 BD_BACKEND=rdp browser-skill ...` (0 popups, "
        "isolated profile), OR\n"
        "  * set `BS_FORCE_AUTOCONNECT_INLINE=1` if you really know what "
        "you're doing.\n"
    )


def _check_inline_gate() -> Tuple[bool, str]:
    """Return ``(should_abort, reason)``.

    ``reason`` is a short string for the abort message; empty on proceed.
    The function is split out of ``run()`` so tests can drive it directly.
    """
    # (3) explicit force-override.
    if os.environ.get("BS_FORCE_AUTOCONNECT_INLINE") in {"1", "true", "yes"}:
        return False, ""

    # (4) direct ws override → no daemon, no popup. **But** (F-4d): if the
    # explicit ws points at ``127.0.0.1:9222`` we'd still hit the user's
    # daily Chrome on the autoconnect default port. Fall through to the
    # abort gate in that case unless ``BS_FORCE_AUTOCONNECT_INLINE=1``.
    explicit_ws = (os.environ.get("BS_CDP_WS")
                   or os.environ.get("BU_CDP_WS") or "")
    if explicit_ws:
        if _hardening._ws_targets_default_port(explicit_ws):
            return True, (
                f"BS_CDP_WS={explicit_ws!r} points at the autoconnect "
                f"default port :9222 (the user's daily Chrome). Use a "
                f"different port or set BS_FORCE_AUTOCONNECT_INLINE=1."
            )
        return False, ""

    # (2) Mode B alive → upstream ws is shared.
    try:
        from ..mode_b_client import ModeBClient
        if ModeBClient().is_alive():
            return False, ""
    except Exception:
        # Don't block the agent because of an introspection failure.
        pass

    # Cheap env trip: user explicitly picked autoconnect.
    if os.environ.get("BS_DAEMON_BACKEND") == "autoconnect":
        return True, "BS_DAEMON_BACKEND=autoconnect set explicitly"

    # (5) ask doctor.
    try:
        from ..daemon_client import DaemonClient
        info = DaemonClient().doctor()
    except Exception:  # noqa: BLE001
        return False, ""

    # Daemon rate-limited *us* on the way in (#74). Spec: surface the
    # daemon error verbatim and abort with exit==2; do not retry.
    err = info.get("error") or ""
    if info.get("skill_synthetic") and "rate-limit" in err.lower():
        return True, f"daemon rate-limited the doctor probe: {err}"

    if info.get("skill_synthetic"):
        # Doctor was simply unreachable. We can't prove popup risk; proceed.
        return False, ""

    recommended = info.get("recommended")
    for b in info.get("backends", []) or []:
        if b.get("name") != recommended:
            continue
        if recommended == "autoconnect":
            return True, f"daemon recommends backend={recommended!r} (popup-per-ws)"
        if "popup" in (b.get("ux_cost") or ""):
            return True, (
                f"daemon recommends backend={recommended!r} with "
                f"ux_cost={b.get('ux_cost')!r}"
            )
        break
    return False, ""


def run(stdin: IO[str]) -> int:
    """Execute heredoc input. Returns the desired exit code."""
    code = stdin.read()
    if not code.strip():
        print("usage: browser-skill <<'PY'\\n  print(page_info())\\nPY",
              file=sys.stderr)
        return 1

    # F-4b: production-hardening assertions run before the popup-cost
    # gate. They catch the orthogonal failure mode where the user has a
    # Chrome on :9222 (or a misconfigured daemon resolving to :9222)
    # entirely outside the autoconnect-via-doctor path.
    try:
        _hardening.assert_safe_or_warn()
    except _hardening.ProductionHardeningRefused as e:
        sys.stderr.write(str(e) + "\n")
        return _ABORT_EXIT

    # (1) re-use the Skill REPL daemon when present — single shared ws,
    # no popup-per-call risk. Short-circuits the gate by design.
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

    # (2)-(5): popup-risk gate (Patch A / P0 #75).
    should_abort, reason = _check_inline_gate()
    if should_abort:
        sys.stderr.write(_abort_message(reason))
        return _ABORT_EXIT

    # Otherwise run in-process. Capture stdout so we can also record it in the
    # session history (which propose_solidify reads).
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
