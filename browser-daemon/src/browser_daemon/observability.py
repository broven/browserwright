"""Observability (v0.5): metrics counters + structured JSON logging.

Spec §7 v0.5 line 2 — "observability / metrics / structured logging".

Three pieces:

1. **Counters** (`Metrics`) — bucketed integer counters incremented inline
   at hot-path call sites (client connect, upstream open, pre-open buffer
   overflow, etc.). Pure dataclass; reads / writes don't lock because
   asyncio gives us single-threaded mutation guarantees within the daemon
   process.

2. **JSON log formatter** — opt-in via `BD_LOG_JSON=1`. Emits one JSON
   object per log record, schema:

       {"ts": "...", "level": "...", "logger": "...", "msg": "...",
        "extra": {...optional structured kwargs...}}

   The default human formatter stays unchanged.

3. **`stats` snapshot** — `snapshot()` returns a dict-of-dicts the
   `browser-daemon stats` CLI subcommand serializes to JSON. Used for
   external monitoring (`watch -n 5 'browser-daemon stats --json'`) and
   in tests to assert hot paths actually incremented their counters.

Design constraints:
- No external metrics deps (prometheus_client, opentelemetry). The daemon
  is supposed to be lightweight (§8.5 "no logging framework").
- Counters are coarse on purpose — every counter has a clear hot-path
  call site. We don't try to time-bucket / histogram / export over the
  wire. That's all v0.6+ territory.
"""
from __future__ import annotations

import json
import logging
import os
import sys
import time
from dataclasses import dataclass, field, asdict


# ---- counters --------------------------------------------------------------


@dataclass
class Metrics:
    """All daemon counters live here. One instance per daemon process,
    accessed via `metrics()` singleton.

    Naming convention: `<area>_<event>` (snake_case). Areas are stable —
    `client`, `upstream`, `proxy`, `auth` (four groups; v0.5.3 F-14 dropped
    the stale `relay_*` mention from this docstring — relay activity is
    counted under `proxy_*` / `upstream_*` instead). Adding a counter is a
    minor version bump for the `stats --json` schema; renaming one is
    major.
    """
    started_at: float = field(default_factory=time.time)

    # ---- client (downstream skill connections) ----
    client_connected_total: int = 0
    client_disconnected_total: int = 0
    client_frame_received_total: int = 0

    # ---- upstream (Chrome / cloud / relay) ----
    upstream_open_attempts_total: int = 0
    upstream_open_succeeded_total: int = 0
    upstream_open_failed_total: int = 0
    upstream_closed_total: int = 0
    upstream_frame_received_total: int = 0
    upstream_frame_sent_total: int = 0

    # ---- proxy (router level) ----
    proxy_attach_succeeded_total: int = 0
    proxy_attach_rejected_total: int = 0
    proxy_pre_open_buffered_total: int = 0
    proxy_pre_open_overflow_total: int = 0
    proxy_pre_open_drained_total: int = 0

    # ---- auth (v0.5 cloud backend) ----
    auth_headers_resolved_total: int = 0
    auth_resolution_failures_total: int = 0

    def snapshot(self) -> dict:
        """Return a flat dict suitable for JSON serialization.

        Includes `uptime_seconds` derived from `started_at`.
        """
        d = asdict(self)
        d["uptime_seconds"] = round(time.time() - self.started_at, 3)
        return d

    def reset(self) -> None:
        """Re-init every counter back to 0 + started_at to now. Mostly a
        test seam — production daemons rotate by restart, not by reset."""
        for k in list(self.__dataclass_fields__.keys()):
            if k == "started_at":
                self.started_at = time.time()
            else:
                setattr(self, k, 0)


_singleton: Metrics | None = None


def metrics() -> Metrics:
    """Lazy singleton accessor. The first call creates the instance; every
    subsequent call returns the same one."""
    global _singleton
    if _singleton is None:
        _singleton = Metrics()
    return _singleton


def reset_metrics_for_test() -> None:
    """Test seam — wipes the singleton so each test starts at zero."""
    global _singleton
    _singleton = None


# ---- JSON log formatter ---------------------------------------------------


class JSONLogFormatter(logging.Formatter):
    """Emit one JSON object per log record. Keeps log lines greppable by
    field (`jq '.msg'`) and friendly to log aggregators.

    Schema (stable in v0.5):
      ts:     ISO-8601 UTC
      level:  uppercase level name
      logger: logger name (e.g. "browser_daemon.server.proxy")
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
