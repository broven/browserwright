# Development never touches the global daemon, the global binary, or the daily Chrome

Status: accepted (2026-09-02). Rule 5 implemented (#89, via #92); rules 1–4 and 6 implemented (#91). Amended the same day: remote use over the tailnet is a real requirement (a VPS drives this Mac's Chrome), so rule 3 keeps the non-loopback bind and makes it durable instead of removing it; rule 4 became an activity gate rather than a concurrency cap; rule 6 was added after the maintainer's own verification run stopped the global daemon. Supersedes the partial
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

1. **`dev-link` installs under a different name.** It writes wrapper
   scripts `~/.local/bin/browserwright-dev` and `browserwright-daemon-dev`,
   never the unsuffixed names, and removes any unsuffixed link that points
   into a checkout. The wrappers pin the dev runtime: own `XDG_RUNTIME_DIR`,
   `TMPDIR`, ledger root (`BS_HOME`), a fixed dev port block
   (21989/21990/21991, clear of production, playwriter, OpenCLI and the e2e
   block) and, decisively, `BW_DAEMON_URL` pointing at the dev port — the
   port variables alone are not isolation (rule 6). The extension backend is
   exercised with Chrome for Testing and the unpacked `chrome-extension/`
   patched to the dev relay port, the way e2e already does it; never the
   daily Chrome. No skill dir is linked: the skill text names the
   `browserwright` binary, so a dev-named skill would route agents to the
   global install anyway.
2. **`upgrade-global` refuses while sessions are active.** It drops
   `--force` and checks `browserwright-daemon activity` before changing the
   global package or extension. A busy result exits 4 with the session names;
   neither installation nor restart begins. The final unforced `restart`
   repeats the same guard so a session that becomes busy during installation
   is protected from the check/install race. After installation, the task
   compares the installed CLI version with `status --json`; an already-running
   healthy copy of that exact version makes restart an idempotent no-op, while
   a missing or different daemon takes the ordinary unforced restart path.
   Reload and verification continue either way. Immediately after the package
   install, an explicit second activity gate runs unconditionally — including
   on the same-version path — before status, extension work, reload, or
   verification. If extension bytes changed, a third explicit gate runs
   immediately before reload, covering activity that began during download;
   `restart` retains its own internal guard for the remaining race.
   Every post-install browserwright/browserwright-daemon command uses the
   absolute `~/.local/bin` production entrypoint instead of PATH, and every
   activity, status, restart, reload, and version command clears inherited dev
   endpoint/config/runtime/port variables before addressing production, while
   setting `TMPDIR` to macOS's canonical per-user temporary directory so
   lifecycle state and logs do not fall back to shared `/tmp`. This deliberately
   tightens the
   original "install first, then let restart refuse" implementation: refusal
   now leaves the entire global install untouched. A human who really wants
   to interrupt live sessions runs
   `browserwright-daemon restart --force` themselves, in a terminal. No task,
   script, or skill document may pass `--force`.
3. **The LaunchAgent is generated, never hand-edited, and remote use is
   durable.** `browserwright-daemon install` owns the plist. Remote use over
   the tailnet is a real requirement, so `--facade-host <tailnet-ip>` stays
   — but as an `install` argument that `install --force` carries forward
   from the installed plist for every flag not given on the command line
   (`plist_serve_args`; pass a flag to override just that one), never as a
   hand edit that the next regeneration silently drops. Two consequences
   for local clients: the facade co-binds loopback (PR #78), and the daemon
   **publishes the loopback address** in its endpoint state file, so a local
   client never resolves the tailnet IP and a VPN outage only affects the
   remote side. Remote clients set `BW_DAEMON_URL` explicitly and never read
   that file.
4. **e2e does not start while production is busy.** The ports are
   isolated, the CPU is not. Rather than a concurrency cap (which would not
   have stopped 20 leaked executors from competing), the e2e runner asks the
   machine-global daemon `browserwright-daemon activity` — the same gate
   `restart` uses — and refuses with exit 4 while any session is mid-task.
   `E2E_FORCE=1` overrides for a human. The rule is still the outcome: an
   e2e run must not measurably slow a production session.
5. **Every daemon start, stop, and restart is attributed.** The daemon log
   gains timestamps and, for each lifecycle event, the initiator (launchd
   spawn, CLI verb with its cwd and parent process, self-exit watchdog). This
   is the evidence that was missing when this ADR was written; two
   subagents and several hours were needed to reconstruct the table above.
6. **`serve` and `stop` are keyed on their own port, not on whatever this
   shell resolves.** `serve` stale-detects against the port it is about to
   bind, on the address local clients use for it; `stop` refuses when the
   config overrides the facade port but the implicitly resolved endpoint
   names a different one. Found the hard way: an "isolated" shell with the
   port variables set but no `BW_DAEMON_URL` made `serve` defer to the
   global daemon and `stop` kill it.
   `stop` skips the guard when the override is port 0 (the per-test
   "bind anywhere" scheme), where the state file is the only truth.

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
- `upgrade-global` can fail with "sessions active" before installing anything.
  That is the intended behavior; wait for the sessions to become idle and run
  the whole task again, rather than adding `--force` back.
- `browserwright-daemon status --json` now reports the loopback URL as the
  endpoint on a tailnet-bound daemon; the tailnet address is still visible
  in `cdp_surface.ws`. Remote clients were never meant to read the state
  file.
- Once implemented, the daemon log's `already running` flood should stop. If
  it does not, the remaining initiator is visible by name (rule 5).
