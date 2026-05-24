# Logging Guidelines

> Standard-library `logging` only — no loguru, no logging framework, no `print()`
> in library code. Per-module loggers, optional JSON output, and coarse in-process
> metrics counters for hot paths.

---

## Overview

- **Library**: Python stdlib `logging`. By design there is "no logging
  framework" and no external metrics deps (prometheus/opentelemetry) —
  see `src/browserwright/daemon/observability.py` docstring.
- **Logger acquisition**: modules that log obtain a module-level logger with

  ```python
  import logging
  logger = logging.getLogger(__name__)
  ```

  This yields dotted names like `browserwright.daemon.server.proxy`, which the
  JSON formatter preserves for filtering. (Not every module defines a logger —
  only those that actually log; the daemon/server modules consistently do.)
- **Prefer the logger over `print()` for diagnostics.** A handful of intentional
  `print()` calls exist for user-facing/CLI output and remain fine: the install
  wizard (`install.py`), deprecation warnings to stderr (`daemon/config.py:285`),
  and a few one-off operator messages (`daemon/server/listener.py:92`,
  `primitives/site.py:58`). New routine diagnostics should go through the logger,
  not `print()`.

---

## Log Levels

| Level | When |
|-------|------|
| `DEBUG` | frame routing, attachment details — high-volume internals |
| `INFO` | lifecycle events: client connect/disconnect, upstream ready, idle close |
| `WARNING` | recoverable anomalies: stale daemon, attachment conflicts |
| `ERROR` | CDP errors, backend resolution failures |

---

## Structured Logging

Plain human-readable format is the default. **JSON is opt-in** via the
`BD_LOG_JSON` env var (`"1"`, `"true"`, or `"True"`), installed by
`install_json_logging_if_requested()` (`observability.py:161`). The
`JSONLogFormatter` emits one object per record:

```json
{"ts": "2026-05-24T...Z", "level": "INFO",
 "logger": "browserwright.daemon.server.proxy", "msg": "...", "extra": {...}}
```

To attach structured fields, pass `extra=`:

```python
logger.info("client connected", extra={"client_id": 7})
```

Any non-standard `record.__dict__` keys (not starting with `_`) land under the
`extra` object; `exc_info` is rendered into an `exc_info` string. Use `extra=`
for queryable fields rather than interpolating them into the message string.

---

## Metrics Counters

Hot paths increment coarse integer counters on the `Metrics` dataclass
(`observability.py:46`), accessed via the `metrics()` lazy singleton. They are
mutated inline without locks (single-threaded asyncio) and exposed through
`browserwright-daemon stats [--json]` (`Metrics.snapshot()`).

- **Naming**: `<area>_<event>` snake_case, where area ∈ `client`, `upstream`,
  `proxy`, `auth` (e.g. `client_connected_total`, `proxy_pre_open_overflow_total`).
- **Schema stability**: adding a counter is a minor bump; **renaming one is a
  major bump** for the `stats --json` schema — pick names deliberately.
- Increment at a clear hot-path call site; don't add histograms / time-bucketing
  (explicitly out of scope).

---

## What NOT to Log

- Secrets / auth headers / credentials — the cloud backend resolves auth
  headers; log the *fact* of resolution (`auth_headers_resolved_total`), not the
  header values.
- Full page content or screenshots.
- Don't log per-frame bodies at `INFO`; that's `DEBUG` territory.
