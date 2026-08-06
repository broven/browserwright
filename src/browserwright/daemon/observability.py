"""Observability: structured JSON logging.

**JSON log formatter** — opt-in via `BD_LOG_JSON=1`. Emits one JSON
object per log record, schema:

    {"ts": "...", "level": "...", "logger": "...", "msg": "...",
     "extra": {...optional structured kwargs...}}

The default human formatter stays unchanged.
"""
from __future__ import annotations

import json
import logging
import os
import sys
import time


# ---- JSON log formatter ---------------------------------------------------


class JSONLogFormatter(logging.Formatter):
    """Emit one JSON object per log record. Keeps log lines greppable by
    field (`jq '.msg'`) and friendly to log aggregators.

    Schema (stable in v0.5):
      ts:     ISO-8601 UTC
      level:  uppercase level name
      logger: logger name (e.g. "browserwright.daemon.server.proxy")
      msg:    formatted message
      extra:  any non-standard `record.__dict__` entries the caller added
              via `logger.info("...", extra={"client_id": 7})`
    """

    _STANDARD = {
        "name", "msg", "args", "levelname", "levelno", "pathname",
        "filename", "module", "exc_info", "exc_text", "stack_info",
        "lineno", "funcName", "created", "msecs", "relativeCreated",
        "thread", "threadName", "processName", "process", "asctime",
        "message",
    }

    def format(self, record: logging.LogRecord) -> str:
        payload: dict = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(record.created)),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        extra = {k: v for k, v in record.__dict__.items()
                 if k not in self._STANDARD and not k.startswith("_")}
        if extra:
            payload["extra"] = extra
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str, ensure_ascii=False)


def install_json_logging_if_requested(stream=None) -> bool:
    """If `BD_LOG_JSON=1`, replace stderr / file handlers' formatter with
    `JSONLogFormatter`. Idempotent. Returns True iff anything changed.

    `stream` defaults to `sys.stderr` (the daemon's normal log channel).
    Callers can override for tests.
    """
    if os.environ.get("BD_LOG_JSON", "") not in ("1", "true", "True"):
        return False
    stream = stream or sys.stderr
    formatter = JSONLogFormatter()
    root = logging.getLogger()
    # If no handlers, create one targeting the requested stream.
    if not root.handlers:
        h = logging.StreamHandler(stream)
        h.setFormatter(formatter)
        root.addHandler(h)
    else:
        for h in root.handlers:
            h.setFormatter(formatter)
    return True


# ---- SIGUSR1 stack dump ----------------------------------------------------


def install_sigusr1_traceback(role: str) -> bool:
    """Make ``kill -USR1 <pid>`` dump every thread's stack to the log.

    The motivation is exact: the executor's SIGTERM handler ends in
    ``os._exit(0)`` (`_executor/process.py`), so signalling a *wedged* executor
    destroys the evidence — no traceback, no core, no state. And the daemon's
    hot loops are async, so a hang there is a coroutine parked on an await that
    no log line records. SIGUSR1 is the "look before you kill" channel: it is
    read-only, costs nothing when unused, and works on a process that is far too
    busy to answer an RPC.

    Writes to the daemon log file when one can be opened (an executor is spawned
    with stderr on /dev/null, so stderr alone would be a no-op there), otherwise
    falls back to stderr. The file handle is kept alive for the process lifetime
    on purpose — ``faulthandler`` writes to the raw fd at signal time, so a
    closed file would make the dump vanish.

    Returns True when the handler was installed. POSIX-only; a platform without
    SIGUSR1 (Windows) returns False rather than raising.
    """
    import faulthandler
    import signal

    sig = getattr(signal, "SIGUSR1", None)
    if sig is None:
        return False
    stream = _dump_stream(role)
    try:
        faulthandler.register(sig, file=stream, all_threads=True, chain=False)
    except (OSError, ValueError, RuntimeError):
        return False
    _DUMP_STREAMS.append(stream)
    return True


#: Keeps dump files open for the process lifetime (see `install_sigusr1_traceback`).
_DUMP_STREAMS: list = []


def _dump_stream(role: str):
    """Append-mode handle on the daemon log, or stderr when it can't be opened."""
    try:
        from . import _ipc

        path = _ipc.log_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        f = open(path, "a", buffering=1, encoding="utf-8", errors="replace")
        f.write(f"\n--- {role} pid={os.getpid()} armed SIGUSR1 stack dump ---\n")
        return f
    except (OSError, ValueError, ImportError):
        return sys.stderr
