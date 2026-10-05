# One caller deadline, propagated end to end; timeouts have their own exit codes

A code agent sees exactly two kinds of timeout, and both are its own to set:

- **Call deadline**: the whole `browserwright -e` call. It is set on the command
  line (`--timeout <seconds>`, default 90s), and every other caller surface takes
  the same field. It fixes one absolute deadline for the call.
- **Operation timeout**: a single Playwright call inside the code, such as `goto`,
  `click` or `wait_for`. The agent sets it with Playwright's own `timeout=` or
  `page.set_default_timeout()`. When the agent sets nothing, Playwright's own
  default applies: 30s for an action, 60s for browserwright's smart `goto`.

The call deadline is propagated all the way down: executor → daemon relay →
extension → `chrome.debugger`. Each layer may finish *sooner* than the remaining
deadline, but none may outlive it. **No inner layer has its own fixed budget
that can expire before the caller's timeout.** An operation timeout is therefore
`min(the agent's value or Playwright's default, the remaining call deadline)`.

The two kinds fail differently and point to different fixes, so each gets its
own exception and exit code:

| kind | exception | exit code | what happened | agent's next move |
|---|---|---|---|---|
| call deadline | `DeadlineExceeded` | **7** | the executor is fail-stopped and the code did not finish | raise `--timeout`, or split the work |
| operation timeout | `OperationTimeout` (a subclass of Playwright's `TimeoutError`) | **8** | the Playwright call raised; the code can catch it, and the executor survives | raise that call's `timeout=`, or inspect the page (`snapshot()`) |

Both carry a `scope` field and a `[fix]` line naming the knob to turn.
`PageLoadFailed(reason="timeout")` folds into `OperationTimeout`. The
`extension-budget` reason disappears, because there is no longer an inner budget
to blame. Exit code 6 is avoided because `browserwright-daemon` already uses it
for `ChromeBinaryNotFound`.

## Why

[#116](https://github.com/broven/browserwright/issues/116) is the failure this
rules out. The extension capped every `chrome.debugger.sendCommand` at 9s and
the relay at 10s, which [#31](https://github.com/broven/browserwright/issues/31)
had settled as one blanket constant. A `goto` the agent had given 60s died at 9s
on a slow-committing page, with an error that blamed an internal budget the
agent could not see or change. Four layers kept their own timers, and whichever
was shortest won, regardless of what the caller asked for.

## Considered options

- **Expose every layer's budget as configuration** (env/toml, including the
  extension's 9s). Rejected: it adds more knobs, and the agent cannot know which
  one applies. "The timeout I gave" is the only concept the agent should hold.
- **Inherit the call deadline as the default for unset operation timeouts.**
  Rejected: a wrong selector would wait out the whole deadline (minutes) before
  failing. Models already expect Playwright's 30s default. The deadline is a
  ceiling, not a default.
- **Auto-extend the call deadline when the code passes a larger `timeout=`.**
  Rejected: it would require parsing code or moving a deadline mid-flight, and a
  stray `timeout=10**9` would wedge the executor indefinitely.
- **Drop the call deadline and rely on operation timeouts.** Rejected: nothing
  could then reclaim an infinite loop or a stuck synchronous call. Fail-stop on
  the deadline stays.
- **One shared timeout exit code with a `scope` field.** Rejected: the agent
  would have to parse the error text to choose between `--timeout` and
  `timeout=`.

## Consequences

- The extension's per-command budget is no longer a constant. The caller's
  remaining deadline travels with each command. The pairing test that locks
  `DEBUGGER_COMMAND_TIMEOUT_MS` below relay `send_cdp` becomes a test that every
  layer's wait is derived from the propagated deadline, and that the extension
  still answers before the relay gives up.
- Daemon-internal operations that have no caller (attach on connect, teardown,
  heartbeats; see ADR-0009 and ADR-0013) keep their own budgets. This ADR
  governs work done on behalf of an agent's call.
- The runtime skill guide must document both knobs, the `min(...)` rule, and
  exit codes 7 and 8.
