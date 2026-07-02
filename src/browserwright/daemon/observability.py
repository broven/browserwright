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
