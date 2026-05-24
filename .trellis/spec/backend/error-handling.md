# Error Handling

> Two exception hierarchies — one for the agent/skill layer, one for the
> daemon — both mapping to **process exit codes**. The defining principle: every
> skill error carries an actionable `fix` string so an agent reading the error
> has a recovery step, not just a raw protocol message.

---

## Overview

- **Skill layer** raises `BrowserwrightError` subclasses (`src/browserwright/errors.py`).
- **Daemon layer** raises `DaemonError` subclasses (`src/browserwright/daemon/errors.py`).
- Errors are not returned as JSON-RPC envelopes. The wire format between daemon
  and clients is **raw CDP-style** frames (`{"id": N, "result": {...}}` /
  `{"id": N, "error": {...}}`). Process-level failures are signaled by exit code.

---

## Error Types

### Skill hierarchy (`errors.py`)

Root `BrowserwrightError(Exception)` — `exit_code = 3`, `default_fix = ""`.
The constructor folds the `fix` into `__str__` (`f"{msg}  [fix: {self.fix}]"`) so
an agent that only logs the message still sees the next action.

| Class | exit_code | Carries |
|-------|-----------|---------|
| `PageLoadFailed` | 3 | `url`, `reason` |
| `ElementNotFound` | 3 | `selector`, `timeout` |
| `NetworkError` | 3 | `url`, `status` |
| `CDPError` | 3 | `method`, `params`, `cdp_message` |
| `AuthWall` | 4 | `url`, `signals` |
| `Captcha` | 5 | `kind`, `url` |
| `DaemonUnavailable` | 2 | `detail` |
| `NoSession` | 2 | `detail` |
| `NeedsUserConfirm` | 1 | `what`, `proposal` |

Each subclass sets a class-level `default_fix` (e.g. `AuthWall.default_fix =
"stop and ask the user to log in; do not type credentials from a screenshot"`).
An explicit `fix=` at the raise site overrides the default.

### Daemon hierarchy (`daemon/errors.py`)

> **Mechanism differs from the skill layer.** Daemon exception classes do **not**
> carry an `exit_code` attribute — `daemon/errors.py` defines only the classes +
> docstrings. The class→exit-code mapping below is applied by the CLI's top-level
> exception handler (`daemon/cli.py:41`), not by the exceptions themselves. The
> codes are documented in the `daemon/errors.py` module docstring.

| Class | exit code (assigned by `daemon/cli.py`) | Notes |
|-------|-----------|-------|
| `DaemonError` | — | base |
| `UserError` | 1 | bad CLI input (unknown backend, invalid flags) |
| `Unavailable` | 2 | no backend resolved a ws URL; carries `attempts: dict[backend → reason]` |
| `ChromeBinaryNotFound` | 6 | subclass of `Unavailable`; launch-chrome found no Chrome |
| (uncaught) | 3 | anything not mapped above |

---

## Error Handling Patterns

- **Make new errors actionable.** When adding a `BrowserwrightError` subclass,
  give it a `default_fix` describing the concrete recovery command/verb. This is
  a project invariant, not a style preference.
- **Choose the right exit code.** Auth/captcha/confirm have dedicated codes
  (4/5/1) so callers can branch; generic script failures are 3. Don't reuse 3
  for something a caller should special-case.
- **`Unavailable` aggregates.** When backend resolution fails, populate
  `attempts` with one entry per backend tried so the CLI can show every
  candidate's reason — don't collapse to a single opaque message unless
  `--backend` was explicit.
- **CDP failures** surface as `CDPError`; a `-32601` (unknown method) maps to a
  "daemon is likely stale — stop and re-run" fix because that's the usual cause.

---

## Error Serialization

`errors.serialize(exc)` (`errors.py:140`) produces the compact dict written to
stderr / the repl socket:

```python
{"type": type(exc).__name__, "msg": str(exc), ...}  # plus any present fields
```

It copies a fixed allow-list of attributes if present and JSON-serializable:
`url, selector, timeout, reason, signals, kind, status, detail, site, task,
failed_check, method, cdp_message, what, proposal, fix`. **If you add a new
field to an exception that callers should see, add it to this allow-list** —
otherwise it is silently dropped from the envelope.

---

## Common Mistakes

- Raising a bare `BrowserwrightError` / new subclass with no `default_fix` — the
  agent gets a message but no recovery step.
- Adding an exception field but forgetting the `serialize()` allow-list, so it
  never reaches the caller.
- Returning ad-hoc `{"error": "..."}` dicts instead of raising a typed exception.
- Picking exit code 3 for a condition (auth/captcha/unavailable) that already
  has a dedicated code.
