"""Length-framed JSON request/response for the executor data plane (Fork 2).

A deliberately simple wire format of OUR design — it does NOT pretend to be CDP
(unlike the mode_b tunnel). Each message is a 4-byte big-endian unsigned length
prefix followed by that many bytes of UTF-8 JSON. The thin heredoc client sends
exactly one :class:`ExecuteRequest`; the executor replies with exactly one
:class:`ExecuteResponse`.

PR3 completes the response: ``console`` / ``return_value`` / ``warnings`` /
``screenshots`` / ``truncated`` / ``error`` (with a traceback for generic
exceptions, mirroring the in-process path), and the ``timeout_ms`` field is now
ENFORCED executor-side (a wedged call returns a timeout error without blocking
the serial queue forever).
"""
from __future__ import annotations

import json
import socket
import struct
from dataclasses import dataclass, field
from typing import Any

# Default per-call timeout (ms). Playwriter defaults to 10000ms, but real page
# ops (cold navigation + network settle) can legitimately take longer, so we
# pick a more generous default. It is deliberately bounded WELL UNDER any
# realistic idle-reap threshold (`Config.idle_close_after`, default None = never)
# so a slow-but-legitimate call never trips idle reclamation mid-flight.
DEFAULT_TIMEOUT_MS = 30000

# Cap on the rendered text block (console + return value), mirroring
# playwriter's ~10000-char truncation. Whole-line aware truncation lives in
# `snapshot._truncate_lines`; here we cap the console blob so a runaway print
# loop can't ship megabytes back to the agent.
MAX_TEXT_CHARS = 10000

_LEN = struct.Struct(">I")
_MAX_FRAME = 256 * 1024 * 1024  # generous: screenshots land here in PR3


@dataclass
class ExecuteRequest:
    """A code blob the thin client ships to the executor."""

    code: str
    timeout_ms: int = DEFAULT_TIMEOUT_MS

    def to_dict(self) -> dict[str, Any]:
        return {"code": self.code, "timeout_ms": self.timeout_ms}

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ExecuteRequest":
        code = d.get("code")
        if not isinstance(code, str):
            raise ValueError("ExecuteRequest.code must be a string")
        timeout = d.get("timeout_ms", DEFAULT_TIMEOUT_MS)
        if not isinstance(timeout, int) or timeout <= 0:
            timeout = DEFAULT_TIMEOUT_MS
        return cls(code=code, timeout_ms=timeout)


@dataclass
class ExecuteResponse:
    """The executor's reply to one :class:`ExecuteRequest` (PR3 full shape).

    Mirrors playwriter's single response object:

      - ``console``: captured stdout/stderr of the run.
      - ``return_value``: ``repr`` of the trailing bare expression (if the last
        statement was an expression), else None — playwriter's ``[return
        value]`` block.
      - ``warnings``: human-facing notices (e.g. a popup that became a tab) the
        client renders as ``[WARNING] …`` lines. The field + plumbing exist
        even though few producers exist yet.
      - ``screenshots``: list of ``{"path": str, ...}`` blocks for any image the
        heredoc captured — path-based (the executor and client share a
        filesystem), so the (possibly large) bytes never ride the wire.
      - ``truncated``: True when the text block was capped at ``MAX_TEXT_CHARS``.
      - ``error``: ``errors.serialize(exc)`` (or None on success), WITH a
        ``traceback`` key for generic exceptions so a shipped heredoc surfaces
        the same traceback the in-process path writes.
      - ``exit_code``: mirrors the heredoc's desired process exit code so the
        thin client can propagate it.
    """

    console: str = ""
    return_value: str | None = None
    error: dict[str, Any] | None = None
    exit_code: int = 0
    warnings: list[str] = field(default_factory=list)
    screenshots: list[dict[str, Any]] = field(default_factory=list)
    truncated: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "console": self.console,
            "return_value": self.return_value,
            "error": self.error,
            "exit_code": self.exit_code,
            "warnings": self.warnings,
            "screenshots": self.screenshots,
            "truncated": self.truncated,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ExecuteResponse":
        return cls(
            console=str(d.get("console") or ""),
            return_value=d.get("return_value"),
            error=d.get("error"),
            exit_code=int(d.get("exit_code") or 0),
            warnings=list(d.get("warnings") or []),
            screenshots=list(d.get("screenshots") or []),
            truncated=bool(d.get("truncated") or False),
        )


# ---- length-framed transport ----------------------------------------------


def send_message(sock: socket.socket, payload: dict[str, Any]) -> None:
    """Send one length-framed JSON message over a blocking socket."""
    body = json.dumps(payload).encode("utf-8")
    sock.sendall(_LEN.pack(len(body)) + body)


def _recv_exact(sock: socket.socket, n: int) -> bytes:
    """Read exactly ``n`` bytes or raise ``ConnectionError`` on early EOF."""
    chunks: list[bytes] = []
    remaining = n
    while remaining > 0:
        chunk = sock.recv(remaining)
        if not chunk:
            raise ConnectionError("executor socket closed mid-message")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def recv_message(sock: socket.socket) -> dict[str, Any]:
    """Read one length-framed JSON message. Raises ``ConnectionError`` on a
    clean peer close before any bytes, ``ValueError`` on a corrupt frame."""
    header = b""
    while len(header) < _LEN.size:
        chunk = sock.recv(_LEN.size - len(header))
        if not chunk:
            raise ConnectionError("executor socket closed before a message")
        header += chunk
    (length,) = _LEN.unpack(header)
    if length <= 0 or length > _MAX_FRAME:
        raise ValueError(f"executor frame length out of range: {length}")
    body = _recv_exact(sock, length)
    return json.loads(body.decode("utf-8"))
