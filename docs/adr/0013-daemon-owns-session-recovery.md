# The daemon owns one recovery state machine per session; agents get one recovery verb

Status: proposed (2026-09-02); rules 3 and 4 implemented (#92). Decided in a
grilling session; the state machine (rules 1, 2, 5) is to be implemented after
ADR-0012 removes the development-induced daemon churn, so that the remaining
failures are the ones this ADR is for.

## Context

Between 2026-05-19 and 2026-09-02 the repo fixed 19 distinct root causes in
the disconnect family, across every layer of the chain
(CLI → daemon → executor → exec relay → cdp surface → extension relay →
MV3 service worker → `chrome.debugger` → tab). Fifteen were independent
defects. Four were second or third passes over the same two surfaces:

- **MV3 service-worker liveness** (keepalive mechanism, staleness sweep,
  socket-identity misbinding, hung `chrome.storage` read);
- **the executor's `page` vs. the real tab** (`switch_tab`, `read_markdown`,
  SW-loss self-heal, killed tab). PR #87's own commit body notes that the
  recovery code from PR #69 "was already there" but unreachable on the common
  path.

Every one of those fixes aimed at *not disconnecting*. None aimed at
*recovering deterministically*. Meanwhile the chain has eight long-lived
components, each with its own heartbeat, staleness threshold, and reconnect
budget (SW: 20 s ping / 25 s stale; relay: 5 s ping / 30 s stale / 35 s
reconnect wait; executor: one rebind per call; daemon: throttled
`recoverSession` sweep on every hello; launchd: KeepAlive). Each decides on its
own whether "the other side is dead". Nothing answers the question an agent
actually has: *which layer is broken for my session, and what is the one
thing I should do?*

The error surface reflects that. Remediation hints point at seven different
actions from nine files: `session reset`, `session new`, `session end`,
`session attach-active`, `doctor`, `browserwright-daemon serve`,
`browserwright-daemon restart`. In the recorded incidents agents followed
them literally:

- the endpoint-resolution hint said `restart` when the daemon was healthy and
  the client had connected to the wrong address (a local proxy answered 503);
  the restart took out another agent's session three minutes later;
- the doctor hint said `serve` on a launchd-managed machine, which always
  fails with "already running" and leads to `restart`;
- "or start a new session" was the legal exit that grew the ledger to 640
  sessions before PR #69;
- the maintainer's description of recovery in practice: agents "create
  sessions frantically, call restart daemon, or just give up on the browser".

Goal chosen: **deterministic recovery**, not zero disconnects. Chrome reaping
the service worker, users closing tabs, and Chrome updates are outside our
control; a recovery that converges from any state is not.

## Decision

### 1. The daemon holds an explicit per-session state machine

The daemon is the only component that simultaneously sees the extension
socket, the executor process, and the CLI request, so it is the only one that
can rule between "wait for the SW to reconnect", "rebind the tab", and
"cold-start the executor". States, at minimum:

`healthy` · `extension-disconnected` · `tab-gone` · `executor-unbound` ·
`executor-dead` · `needs-human`

Every hop reports events *to* the daemon; no hop decides recovery on its own
anymore. The relay's staleness sweep, the executor's rebind, and the hello
sweep become inputs to this machine, not parallel authorities.

**The state lives on disk, the daemon process is replaceable.** On start the
daemon rebuilds every session's state from the ledger and from the executor
pid records that #40 already introduced, and **adopts** executors that are
still alive rather than assuming an empty world. This reverses ADR-0011's
accepted regression that a daemon restart severs the live data plane. The
transport stays single-endpoint; only the boot-time assumption changes.

### 2. Recovery is implicit first, explicit as the fallback

Every command first lets the daemon converge the session to `healthy`; an
agent normally sees a slow call, not an error. When convergence fails inside
the client's deadline, the error names the state and exactly **one**
instruction:

```
browserwright recover --session <id>
```

`recover` is a new verb that runs the escalation ladder for one session:
wait for the extension to reconnect (bounded by the relay's reconnect
window) → rebind the session's tab → cold-start the session's executor →
self-check the daemon. It affects one session only.

The last rung may restart the daemon **automatically** when, and only when,
the daemon's self-check finds one of: the daemon process is gone; the
production port is held by something that does not answer the daemon's own
ping; the running version differs from the installed version. Because a
mistaken "port held by a stranger" verdict would recreate the restart loop
this ADR is trying to end, that probe must agree on two consecutive attempts
before it counts. Every automatic restart is logged with its reason (ADR-0012
rule 5). If the ladder ends in `needs-human`, `recover` says so with the
diagnosis and stops; it never loops.

### 3. Agent-visible error text follows a banned-word list

Text an agent can see (exceptions, `doctor`, skill documents' runtime-error
sections) must not contain `restart`, `serve`, `--force`, or `session end` as
a remediation. `session new` appears only in the "you have no session"
error, and there in the form
`browserwright session new --reuse --backend=<b> --name=<label>`.

Before raising any "X is unavailable" error, the client probes X itself once
(`/__ping__` on the endpoint, the state-file alternative address) and writes
the result into the message. "Connection refused" and "something else
answered" are different diagnoses with different next steps and must not be
merged into one hint.

### 4. `session new --reuse`

`--reuse` returns the existing session id when a session with the same
`--name` and backend exists and is `healthy` or recoverable, printing which
id was reused. Without the flag `session new` behaves as today. The flag is
opt-in by the maintainer's choice; the runaway-creation guard is therefore
the error text (rule 3), not a rate limit.

### 5. `browserwright-daemon restart` gates on diagnosis, not on session count

Today `restart` refuses while sessions are active and agents comply by ending
sessions first. The new gate: `restart` runs the daemon self-check from rule 2
and refuses when the daemon is healthy, naming the layer that is actually
broken and the `recover` command for it. `--force` stays for humans and is
not documented for agents (ADR-0012 rule 2).

## Explicitly rejected

- **Zero disconnects as the goal.** Not achievable; it is what the previous
  19 fixes pursued.
- **Recovery authority in the executor.** It is the first thing severed on
  daemon restart.
- **Reverting ADR-0011 to executor direct-connect** to survive restarts.
  Re-creates the multiple-transport problem it solved; adoption on boot gets
  the same property.
- **Rate-limiting automatic daemon restarts** and **a soft cap on session
  creation per cwd.** Both offered, both declined by the maintainer in favor
  of the two-probe rule and error text respectively.
- **Session reuse by default.** Declined; explicit `--reuse`.

## Consequences

- Sessions survive a daemon upgrade or crash with a few seconds of latency
  instead of `ExecutorUnavailable`. Note this makes ADR-0012's isolation
  less load-bearing over time, not redundant: resource starvation from a
  parallel e2e run is not something adoption fixes.
- `status` and `doctor` gain a per-session state column that says which layer
  is broken. This is the observability that was missing in every incident
  reviewed.
- The eight heartbeat/timeout budgets stop being independent policies and
  become tunables of one machine; they should be documented in one table.
- The extension relay's `recoverSession` sweep and the executor's
  `TERMINAL_TARGET_CLOSED` self-heal are folded into the state machine rather
  than deleted; the mechanisms are sound, their coordination was not.
- Risk: the automatic-restart rung can still misjudge. The two-probe rule and
  attributed logging are the mitigations; if a restart loop appears in the
  log, the first suspect is that verdict.

## Sequencing

1. ADR-0012 (isolation, #91) and the error-text / probe / logging work (rule 3 and
   ADR-0012 rule 5: #88, #89, #90) land first. They need no state machine.
2. Boot-time adoption of live executors (rule 1, second paragraph).
3. The state machine, `recover`, `--reuse`, and the `restart` gate.
