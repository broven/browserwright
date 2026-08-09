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
import re
import socket
import struct
from dataclasses import dataclass, field
from typing import Any

from .._text import MAX_TEXT_CHARS as _MAX_TEXT_CHARS

# Default per-call timeout (ms). Playwriter defaults to 10000ms, but real page
# ops (cold navigation + network settle) can legitimately take longer, so we
# pick a more generous default. It is deliberately bounded WELL UNDER any
# realistic idle-reap threshold (`Config.idle_close_after`, default None = never)
# so a slow-but-legitimate call never trips idle reclamation mid-flight.
DEFAULT_TIMEOUT_MS = 90000

# Cap on EVERY text channel of the response — console, return value, warnings
# and the task result JSON — so a runaway print loop, or an equally ordinary
# expression-valued last statement, can't ship megabytes back to the agent.
# Re-exported from `_text` because the producers (`repl/snapshot.py`,
# `repl/markdown.py`) derive their own budgets from it and must not import the
# transport to do so; `process._finish` applies it via `_text.truncate_hard`,
# which prefers whole lines and marks the cut when it cannot keep one.
MAX_TEXT_CHARS = _MAX_TEXT_CHARS
TERMINAL_DEADLINE_EXCEEDED = "deadline_exceeded"
TERMINAL_RESET_REQUESTED = "reset_requested"
TERMINAL_REASONS = frozenset(
    {
        TERMINAL_DEADLINE_EXCEEDED,
        TERMINAL_RESET_REQUESTED,
    }
)

_LEN = struct.Struct(">I")
_MAX_FRAME = 256 * 1024 * 1024  # generous: screenshots land here in PR3
_ENV_NAME_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")


def is_valid_env_name(name: str) -> bool:
    """Return whether ``name`` is a portable process-environment key."""
    return bool(_ENV_NAME_RE.fullmatch(name))


def _validate_json_value(value: Any, *, path: str = "task.args") -> Any:
    """Validate and copy a JSON-compatible task argument value."""
    if value is None or isinstance(value, (str, bool, int, float)):
        return value
    if isinstance(value, list):
        return [
            _validate_json_value(item, path=f"{path}[{index}]")
            for index, item in enumerate(value)
        ]
    if isinstance(value, dict):
        copied: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise ValueError(f"{path} keys must be strings")
            copied[key] = _validate_json_value(item, path=f"{path}.{key}")
        return copied
    raise ValueError(f"{path} must contain only JSON-compatible values")


@dataclass
class TaskEnvelope:
    """A validated site-skill invocation carried by the executor protocol."""

    site: str
    name: str
    args: dict[str, Any] = field(default_factory=dict)
    isolated: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "site": self.site,
            "name": self.name,
            "args": _validate_json_value(self.args),
            "isolated": self.isolated,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "TaskEnvelope":
        if not isinstance(d, dict):
            raise ValueError("ExecuteRequest.task must be an object")
        site = d.get("site")
        name = d.get("name")
        args = d.get("args", {})
        isolated = d.get("isolated", False)
        if not isinstance(site, str) or not site.strip():
            raise ValueError("ExecuteRequest.task.site must be a non-empty string")
        if not isinstance(name, str) or not name.strip():
            raise ValueError("ExecuteRequest.task.name must be a non-empty string")
        if not isinstance(args, dict):
            raise ValueError("ExecuteRequest.task.args must be an object")
        if not isinstance(isolated, bool):
            raise ValueError("ExecuteRequest.task.isolated must be a boolean")
        return cls(
            site=site,
            name=name,
            args=_validate_json_value(args),
            isolated=isolated,
        )


@dataclass
class ExecuteRequest:
    """One code blob or validated task invocation shipped to the executor."""

    code: str
    timeout_ms: int = DEFAULT_TIMEOUT_MS
    env: dict[str, str] = field(default_factory=dict)
    executor_id: str | None = None
    task: TaskEnvelope | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "timeout_ms": self.timeout_ms,
            "env": dict(self.env),
            "executor_id": self.executor_id,
            "task": self.task.to_dict() if self.task is not None else None,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ExecuteRequest":
        code = d.get("code")
        if not isinstance(code, str):
            raise ValueError("ExecuteRequest.code must be a string")
        timeout = d.get("timeout_ms", DEFAULT_TIMEOUT_MS)
        if not isinstance(timeout, int) or timeout <= 0:
            timeout = DEFAULT_TIMEOUT_MS
        env = d.get("env", {})
        if not isinstance(env, dict):
            raise ValueError("ExecuteRequest.env must be an object")
        request_env: dict[str, str] = {}
        for name, value in env.items():
            if not isinstance(name, str) or not is_valid_env_name(name):
                raise ValueError("ExecuteRequest.env contains an invalid variable name")
            if not isinstance(value, str):
                raise ValueError(
                    f"ExecuteRequest.env value for {name!r} must be a string"
                )
            request_env[name] = value
        executor_id = d.get("executor_id")
        if executor_id is not None and (
            not isinstance(executor_id, str) or not executor_id
        ):
            raise ValueError("ExecuteRequest.executor_id must be a non-empty string")
        raw_task = d.get("task")
        task = TaskEnvelope.from_dict(raw_task) if raw_task is not None else None
        if task is not None and code:
            raise ValueError("ExecuteRequest must contain code or task, not both")
        return cls(
            code=code,
            timeout_ms=timeout,
            env=request_env,
            executor_id=executor_id,
            task=task,
        )


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
      - ``truncated``: True when ANY text channel was capped at
        ``MAX_TEXT_CHARS`` — console, return value, warnings or task result.
        Which one it was is said in ``warnings``, the out-of-band channel.
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
    # A terminal disposition belongs to the *executor request*, not to an
    # exception raised by user code.  This is deliberately separate from
    # ``error.type`` so an ordinary Playwright ``TimeoutError`` cannot be
    # mistaken for an outer executor deadline.
    terminal_reason: str | None = None
    # JSON encoding of a task's result.  ``None`` means this was not a
    # successful task response; the JSON string ``"null"`` is a valid result.
    task_result_json: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "console": self.console,
            "return_value": self.return_value,
            "error": self.error,
            "exit_code": self.exit_code,
            "warnings": self.warnings,
            "screenshots": self.screenshots,
            "truncated": self.truncated,
            "terminal_reason": self.terminal_reason,
            "task_result_json": self.task_result_json,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ExecuteResponse":
        terminal_reason = d.get("terminal_reason")
        if terminal_reason is not None and terminal_reason not in TERMINAL_REASONS:
            raise ValueError("ExecuteResponse.terminal_reason is invalid")
        task_result_json = d.get("task_result_json")
        if task_result_json is not None:
            if not isinstance(task_result_json, str):
                raise ValueError("ExecuteResponse.task_result_json must be a string")
            try:
                json.loads(task_result_json)
            except (TypeError, ValueError) as e:
                raise ValueError(
                    "ExecuteResponse.task_result_json must contain valid JSON"
                ) from e
        return cls(
            console=str(d.get("console") or ""),
            return_value=d.get("return_value"),
            error=d.get("error"),
            exit_code=int(d.get("exit_code") or 0),
            warnings=list(d.get("warnings") or []),
            screenshots=list(d.get("screenshots") or []),
            truncated=bool(d.get("truncated") or False),
            terminal_reason=terminal_reason,
            task_result_json=task_result_json,
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
