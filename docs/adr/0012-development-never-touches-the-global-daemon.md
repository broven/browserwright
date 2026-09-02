# Development never touches the global daemon, the global binary, or the daily Chrome

Status: accepted (2026-09-02), not yet implemented. Tracking: #91 (rules 1–4), #89 (rule 5). Supersedes the partial
isolation in `tests/conftest.py` (kept) by extending the same rule to every
developer-facing verb.

## Context

Between 2026-07-19 and 2026-09-02, agents on the maintainer's machine hit 22
disconnect-class failures while driving browserwright (`ExecutorUnavailable`,
`PageBindTimeout`, `DaemonUnavailable`, `TargetClosedError`). We correlated
each incident against the daemon's own history and the agents' transcripts.
Every incident sits within minutes of one of these on the same machine:

| incident | what else was running |
|---|---|
| 09-02 01:26 executor died during cold start; a script looped 606 times | `mise run test:e2e` started 01:20; 20 executors + 31 e2e children alive |
| 09-01 11:19 a Codex session's `ensureExecutor` failed; it gave up on the browser | another agent ran `browserwright-daemon restart` at 11:16 |
| 09-01 12:53 daemon pid changed several times in 30 min | `upgrade-global` 12:31, pytest e2e 12:21, `mise run test` 12:27 and 12:34 |
| 08-09 14:10 `status` reported `alive=False` | restarts at 13:48 and 13:56, `mise run test` 14:01 |
| 08-05 07:38 `executor socket closed before a message` | e2e runs 07:39 and 07:43, restart 07:44 |

In the three weeks (08-10 to 08-31) with no development activity there were
zero incidents. None of the 22 was a failure of the extension link itself
(no `no close frame`, no `chrome.debugger` detach); the last real fix on that
surface landed in June.

The daemon's stderr log confirms the shape: 87,000 lines, almost all
`browserwright-daemon already running (pid N)`, i.e. launchd's KeepAlive
re-spawning a daemon that finds another daemon on its ports. Two daemon
lifecycles have been fighting on this machine for months.

The isolation that exists today is partial:

- `tests/conftest.py` walls the unit suite off correctly (private
  `XDG_RUNTIME_DIR`, endpoint state file pointing at a dead port, spawn
  entry points stubbed, real `Popen` raises). The e2e harness uses its own
  port block and Chrome for Testing. **These are kept as-is.**
- `mise run dev-link` symlinks `~/.local/bin/browserwright-daemon` to the
  current worktree's `.venv`. The LaunchAgent plist runs exactly that path,
  so after one `dev-link` the machine-global "production" daemon *is* the
  checkout under edit, with a version that no longer matches the `uv tool`
  install. Its own description admits "a broken checkout can break global
  agents". Ten agent sessions ran it in 45 days.
- `mise run upgrade-global` runs `browserwright-daemon restart --force`,
  killing every session's live executor. The comment justifies this as "an
  explicit human intent", but the caller in practice is an agent (one
  session ran it five times in 20 hours).
- ADR-0011 accepted that "a daemon restart now severs a live data plane".
  Combined with the above, every development verb became a machine-wide
  outage for every agent using the browser.
- The LaunchAgent was hand-edited to pass `--facade-host 100.72.20.32`
  (a Tailscale address). Until PR #78 that meant the daemon did not listen on
  loopback at all, while a local proxy answered 503 on `127.0.0.1:19990`; the
  resulting error text sent agents to `serve`/`restart`. All agents that use
  this machine's browser run locally, so the flag has no remaining purpose.

## Decision

**A developer verb may not read, write, spawn, signal, or replace anything the
global install owns.** Concretely, "the global install" is:

- the LaunchAgent `com.browserwright-daemon` and the daemon process it manages;
- `~/.local/bin/browserwright` and `~/.local/bin/browserwright-daemon` (the
  `uv tool` install they point at);
- the production ports 19989 / 19990 and the global endpoint state file;
- the user's daily Chrome and the store/iCloud-installed extension;
- `~/.browserwright/` (sessions ledger, site skills, memory).

Rules, one per leak found:

1. **`dev-link` installs under a different name.** It links
   `~/.local/bin/browserwright-dev` and `browserwright-daemon-dev` (and a
   `browserwright-dev` skill dir), never the unsuffixed names. The dev daemon
   runs with its own `XDG_RUNTIME_DIR`, its own ports (a fixed dev block,
   distinct from both production and the e2e block), its own ledger root,
   and drives Chrome for Testing with the unpacked `chrome-extension/`,
   never the daily Chrome. A dev checkout is exercised the way e2e already
   exercises it, just interactively.
2. **`upgrade-global` refuses while sessions are active.** It drops
   `--force` and inherits the daemon's own refusal. A human who really wants
   to interrupt live sessions runs `browserwright-daemon restart --force`
   themselves, in a terminal, after reading the refusal that names the
   sessions. No task, script, or skill document may pass `--force`.
3. **The LaunchAgent is generated, never hand-edited.** `browserwright-daemon
   install` owns the plist; it binds loopback only. Remote use (ADR-0011's
   motivation) is configured through the daemon's own config file, not by
   editing `ProgramArguments`. The current `--facade-host` is removed.
4. **e2e caps its concurrency.** The harness limits simultaneous executors
   and Chrome for Testing instances so an e2e run cannot starve the
   production daemon of CPU. The exact cap is an implementation detail; the
   rule is that an e2e run must not measurably slow a production session.
5. **Every daemon start, stop, and restart is attributed.** The daemon log
   gains timestamps and, for each lifecycle event, the initiator (launchd
   spawn, CLI verb with its cwd and parent process, self-exit watchdog). This
   is the evidence that was missing when this ADR was written; two
   subagents and several hours were needed to reconstruct the table above.

## What this does NOT change

- ADR-0011's single-endpoint transport stays. Whether a daemon restart should
  keep sessions alive is a separate decision (see the planned ADR-0013 on
  daemon-owned session recovery).
- `tests/conftest.py` and the e2e harness already comply; they are the model,
  not the target.
- The extension relay, executor, and cdp surfaces are untouched. This ADR is
  about who is allowed to restart the machine's daemon, not about how the
  daemon recovers.

## Consequences

- A developer loses the convenience of `dev-link` making the checkout *be*
  the global install. Verifying a change against the daily Chrome now means
  releasing it (`upgrade-global`, which will refuse while an agent is mid-task)
  or driving Chrome for Testing with the dev daemon.
- `upgrade-global` can fail with "sessions active". That is the intended
  behavior; the fix is to wait or to end your own sessions, not to add
  `--force` back.
- Remote use needs a config-file path for the bind host before rule 3 can be
  completed; until then rule 3 means "loopback only".
- Once implemented, the daemon log's `already running` flood should stop. If
  it does not, the remaining initiator is visible by name (rule 5).
