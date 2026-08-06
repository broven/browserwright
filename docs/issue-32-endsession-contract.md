# Issue #32 — `endSession` contract: design decision

Status: **implemented** (branch `broven/issue-32-endsession`, PR pending).
Decision: Option A (initiate + poll) with the fast/slow boundary placed after
the executor reap. Summary of the implementation:

- `ExecutorRegistry.terminate_session` returns at the initiate boundary
  (`{ok, initiated, phase: "terminating"}`) and runs the workspace teardown
  as a daemon-side task; a retried `terminate_session` joins it and returns
  the FINAL result; `ensure` is refused from initiate time via a pending
  marker; `await_termination` exposes the final result.
- `Daemon.terminate_session` gained `wait=` (default True — auto-prune and
  embedders keep blocking semantics) and a `_watch_termination` task that
  publishes `ended`/`active` + the result when nobody polls.
- The verb handler passes `wait=False`; the CLI `end-session` handler
  initiates, then re-issues `endSession` to join with visible progress, and
  `browserwright-daemon ps` gained a per-session `sessions` table (phase +
  final result).
- `session_create._run` got a timeout parameter; `session end` subprocess
  timeout covers initiate + join.

## 1. The mismatch, verified

`endSession` is a synchronous request/response verb whose operation's worst
case outlives every caller's timeout. Verified numbers in the current tree:

| Hop | Bound | Where |
|---|---|---|
| Executor reap (TERM → KILL → zombie) | `_KILL_GRACE_S = 3.0` + 1.0s ≈ **≤4s** | `executor_registry.py` |
| Cooperative workspace teardown | `_END_SESSION_BUDGET_S = 8.0` (deadline = 7.5s) | `verbs.py` |
| **Daemon lock-hold worst case** | **≈11.5s** (reap + teardown, serial) | `daemon.terminate_session` |
| CLI `end-session` timeout | **10.0s** | `cli.py` `_FORWARDS` |
| `session_create._run` subprocess timeout | **10.0s** | `session_create.py` |

Underlying physics (why a sync verb is the wrong model even with the cap):
`STALE_FRAME_AFTER = 30.0` + `RECONNECT_WAIT_TIMEOUT = 35.0` per tab close on a
cold extension (`relay.py`), tabs closed **serially**. Pre-#33 the teardown was
unbounded — the issue's "≥60s, minutes" figures. #33 capped the damage but not
the contract: daemon worst case (≈11.5s) still exceeds the caller's 10s.

Reproduction (`tests/daemon/test_repro_gh32_endsession_timeout.py`, real
`_rpc.call` + real `Router._handle_end_session` + real `ExecutorRegistry`
lock) demonstrates the full symptom chain:

1. caller raises `TimeoutError` while the daemon still holds the per-session
   lock and still runs teardown;
2. daemon then **completes** the teardown the caller never saw and installs
   its terminal tombstone **after** the caller reported failure;
3. a concurrent `ensureExecutor` blocks on the held lock, then is refused
   ("session has ended") — while Layer 2 kept the ledger row (any nonzero CLI
   exit keeps it), i.e. a ledger entry whose ordinary operations are refused.

Note the client disconnect does **not** cancel the daemon-side handler
(listener's `async for` surfaces `ConnectionClosed` only after the in-flight
route completes), so the daemon already runs teardown to completion regardless
of the caller — the caller just never finds out.

## 2. Why neither obvious lever works alone

- **Shorten the lock** → gives back the atomicity #33 just bought; the
  concurrent-`ensureExecutor` race (reopen after teardown, before spawn) returns.
- **Lengthen the CLI timeout** → user waits a minute on `session end` with no
  output and no way to tell hung from slow; also merely shrinks, never removes,
  the mismatch (daemon budget is not a hard cap on real browser latency).
- **Cap the teardown budget** → done in #33; bounds damage but a genuinely-slow
  teardown reports partial failure, which is honest but still leaves the caller
  guessing what to do next.

## 3. Options

### A. Async initiate + poll

`endSession` validates, revokes clients, reaps the executor, publishes
phase `terminating`, spawns the workspace teardown as a daemon-side task, and
returns immediately with `{initiated: true, phase: "terminating"}`. The caller
polls the existing whole-daemon `BrowserwrightDaemon.status` verb (the read
side `browserwright-daemon ps` already uses — currently missing only a
per-session `phase`) until `ended` (final result) or `active` (partial).

- **Pros**: matches the real shape of the operation; no caller can ever time
  out mid-teardown; phase + start time make "hung vs slow" observable; the
  daemon finishes what it started (no caller re-ownership needed — see §4);
  the `_session_phases` state machine (`terminating` → `ended`) already exists
  in `daemon.py`; idempotent retry is trivially safe (join the in-flight task).
- **Cons**: the CLI's `session end` becomes initiate+poll (external contract
  preserved: exit 0 only on `ended`); teardown task lifecycle must be defined
  across daemon shutdown (bounded by the existing budget; a shutdown mid-
  teardown leaves today's retry semantics, no worse); `ps` needs a `phase`
  field.

### B. Keep sync; define an explicit partial-teardown contract

Define the ledger/state shape of a half-torn-down session, which verbs are
legal against it, and that a retry *resumes* (the durable retry anchors in
`extension_upstream._persist_retry_anchors` already implement resume).

- **Pros**: no protocol change; response already carries
  `ok/partial/timedOut/closed/failed/unknown`; resume machinery exists.
- **Cons**: the caller still blocks up to the full worst case — the 10s
  timeout still fires on slow teardowns, so the "caller errors out while the
  daemon works" split survives unless the timeout is lengthened (rejected in
  §2). The legal-operations surface against a half-torn-down session is large
  and must be policed per verb. And it leans hardest on the #29 ownership
  anchor (see §4): resuming later requires re-proving ownership of a group
  that is already half-destroyed. B is the status quo's own vocabulary, and
  remains the crash-recovery fallback under A — not the primary contract.

### C. Split fast/slow teardown

Fast phase (executor + bindings, bounded, synchronous) returns success; slow
phase (workspace, idempotent) runs in the background.

- **Pros**: caller wait bounded by the fast phase only (~≤4s, fits 10s with
  margin); atomicity preserved if the tombstone is installed at the fast/slow
  boundary.
- **Cons**: `ok: true` no longer means "tabs are closed" — the caller still
  needs a read side to learn the final outcome, and the daemon needs a
  "terminating, workspace pending" state distinct from both `active` and
  `ended`. **C without polling is dishonest; C with polling is A with the
  sync boundary placed after the reap.** So C is not a third alternative — it
  is the boundary-placement decision inside A.

## 4. Recommendation: A, with the C-boundary after the reap

Adopt **A (initiate + poll)**, with the fast/slow boundary from C: the
initiate response is sent **after** the bounded fast phase (validate → revoke
clients → reap executor → publish `terminating`), and only the **workspace
teardown** (the unbounded part: serial tab closes, 35s reconnect windows)
runs as a daemon-side background task under the same per-session lock, ending
with the tombstone (`ended` + final result) or `active` + partial result.

Why this specific boundary:

- The executor reap is the part that must be *confirmed* before anything else
  may touch the workspace, and it is bounded (~4s) — keeping it synchronous
  preserves today's "reaped: true or refuse" guarantee and keeps the initiate
  call well under the 10s timeout with zero polling.
- The workspace teardown is the unbounded part — backgrounding it removes the
  only place the caller could ever block past its timeout.
- The lock and the tombstone-of-intent (`terminating` published before the
  lock is released) keep the #33 atomicity: a queued `ensureExecutor` is
  refused from the initiate moment, not from the teardown-complete moment.
- The CLI keeps its external contract (`session end` exits 0 only once
  `ended`), but instead of one blind 10s block it does initiate (fast) +
  poll with visible progress ("tearing down… 12s") and a generous overall
  wait — so the user can finally tell slow from hung.

### Relation to #29 (group ownership anchor)

#29 makes the *retry-resume* path fragile: a partially-torn-down extension
group may no longer prove ownership. A's initiate+poll **does not worsen**
this: the daemon runs the teardown it started to completion without needing
the caller to come back and re-prove anything (the poll is a read). The
retry anchors are only needed for the crash-mid-teardown case — exactly
today's semantics, no regression. B (explicit resume contract) and C
(if teardown is allowed to pause and be resumed by a later call) both lean
on the unsound anchor in their *normal* path, which is a further reason to
prefer A.

### Implementation sketch

1. `daemon.terminate_session` → split: fast part stays under
   `_termination_locks[session]` + registry lock; after reap, publish
   `_session_phases[session] = "terminating"`, install a pending marker, and
   hand the teardown callback to a background task that publishes
   `ended`/`active` + `_session_results` on completion. A concurrent/retry
   `endSession` joins the in-flight task instead of racing it.
2. `ExecutorRegistry._raise_if_terminal` → also raise while a pending
   teardown marker exists (ensure cannot slip in between initiate and
   tombstone).
3. `status.snapshot` → expose per-session `phase` (+ teardown start/elapsed
   if cheap); `_pretty_ps` prints it.
4. CLI `end-session` → initiate with the existing short timeout, then poll
   `ps` with visible progress until `ended` (exit 0 + result) / `active`
   (exit 3 + partial result) / overall wait cap.
5. `session_create._run` → end-session call gets a longer subprocess timeout
   (it now legitimately covers initiate + poll).

### Verification plan

- Repro test becomes the regression test: after the fix, the caller **never**
  times out while the daemon still holds the lock (the client receives
  `initiated` promptly; teardown completes under the lock; `ended` is
  pollable; a mid-teardown `ensureExecutor` is refused from initiate time).
- New unit tests: initiate-then-poll happy path; retry-join; partial
  teardown → `active` + partial result; ensure refused during `terminating`;
  CLI progress output.
- Full fast gate (`mise run test`) + lint.
