# CONTEXT — the project's glossary

The words this codebase uses, what each one means, and the trap attached to it.
When a term here and the code disagree, the code is the bug report — fix one of
them, don't invent a third word.

Scope: **domain vocabulary only.** For architecture vocabulary (module,
interface, depth, seam, adapter) see the `/codebase-design` skill. For the
session model's *rules* (invariants, teardown, ownership) see
[`docs/session-workspaces.md`](docs/session-workspaces.md) — this file only
names things.

---

## The model in one sentence

One global **daemon** serves many **sessions**; each session is one code
agent's browser **workspace**, reached through one **upstream** connection and
driven by one resident **executor**.

```
  code agent
      │  downstream
      ▼
 ┌──────────────────────────────── daemon ───────────────────────────────┐
 │  ledger ──► session ──► UpstreamContext { state·Router·holder·adapter}│
 │                                    │                                  │
 │  executor (one per session)        │ upstream                         │
 │  endpoint (/control · /exec · /cdp) │                                 │
 └────────────────────────────────────┼──────────────────────────────────┘
                                      ▼
                        extension relay ──► user's Chrome
                        or raw CDP ws  ──► daemon-owned / external Chrome
```

---

## Core terms

### session
The unit of isolation, and the only durable identity. One code agent gets one
session. The session id travels through the Layer 2 CLI, all three endpoint surfaces, and
the ledger.

**Trap:** a session's `--name` is a *human label*, not an identity key — names
need not be unique. The stable key is the session id. On extension the two are
combined into the group title `<name>-BW<sid>`, and it is the `<sid>` half that
makes it unique (see `binding`).

### workspace
What a session's browser *is*, materially. Backend-specific:

| backend | workspace | isolation boundary |
|---|---|---|
| `extension` | one Chrome tab group inside the user's real Chrome | the tabs in that group |
| `cdp` create | a daemon-owned Chrome instance + profile | the browser instance |
| `cdp` attach | an externally-owned browser at that session's recorded port or URL | the browser instance |

**Trap:** tab groups are the extension workspace **only**. Never create or
simulate them for `cdp`. And a tab group isolates tab membership — not
cookies, localStorage, or login state. All extension sessions share the user's
one Chrome profile.

### backend
`extension` | `cdp`. Chosen at `browserwright session new` and
**immutable for the life of the session**. The daemon reads it from the ledger,
never from the client's environment.

### raw-CDP backend
`cdp` — the backend that speaks real browser-level CDP, whether we launched
the browser (`--create`) or were handed an endpoint (`--attach=<port|url>`).
The discriminator is `Router._raw_cdp_backend`, defined as
`backend != "extension"`, because extension is the sole relay backend.

**Trap:** never write a name check to mean this. The family had two members
(`rdp`, `env`) until #38 merged them, and `backend == "rdp"` silently excluded
the other one for a whole release. It has one member today, so the predicate
looks redundant — keep it anyway: it says *"the browser connection is the
workspace boundary"*, which is the property every caller actually depends on.

### ledger
The durable session registry — one lock-serialized JSON file per `BS_HOME`
(`$BS_HOME/sessions/ledger.json`, `BS_HOME` defaults to `~/.browserwright`).
It is the durable source that outlives daemon replacement and fresh agent
shells. Resident executors may also survive a daemon replacement, but their
identity and health are re-proven from discovery records before adoption.

A record has two tiers, and conflating them is the recurring bug:

| tier | fields | authority |
|---|---|---|
| durable fact | `id` · `backend` · `owner` · `workspace` · `name` · `created_at` · `last_seen` | authoritative — the daemon obeys these |
| recovery observation | `recovery.{state, since, reason}` | daemon-owned diagnosis, rebuilt from live relay/executor facts on boot |
| runtime cache | `runtime.{current_target_id, updated_at}` | a *candidate* — written best-effort, re-proven against the live browser |

Four jobs, all load-bearing:

- **identity** — `next_id` mints session ids. There is no other allocator.
- **routing authority** — the daemon resolves a session's backend and upstream
  context by reading its record, never from a client param or the client's
  environment; `backend` is immutable for the session's life. No record, or a
  record that doesn't match this daemon, fails closed — never a silent
  fallback to the shared context.
- **admission control** — the guards that must be atomic live inside the
  ledger's lock, because check-and-mutate has to be one step: today that is
  `update()`'s backend-immutability guard (`backend` is fixed at creation, see
  `session`).
  A rejected write leaves the ledger byte-identical.
- **idle clock** — `last_seen` advances when a new instruction arrives,
  *deliberately not* with executor liveness (a stuck executor must not keep a
  session alive forever). Auto-prune measures it, and removes a row only after
  that session's workspace teardown is confirmed.

**Trap — `runtime` is a cache, and nothing in it is load-bearing.** Since
ADR-0009 it holds only `current_target_id` and `updated_at`. The extension's
group binding is *not* in here: `runtime.group_id` used to be, and the daemon
used it to find the tab group again after a restart — that job now belongs to
the group title (see `binding`), which needs no durable mirror. Don't add one
back; a cached id that can disagree with the live browser is the shape of bug
this removed.

**Trap — readers bypass the lock.** Reads are unlocked for speed (which is why
writes are atomic: a reader sees the old ledger or the new one, never half of
one). So any check-then-act is a race unless the whole sequence runs inside the
lock — and why the retired-backend sweep runs inside `_locked` rather than as
a read followed by `update()`.

**Trap — it is a credential store.** A record can carry a CDP endpoint with an
embedded bearer token, which is what justifies the owner-only file and
directory. Don't add a field, log line, or debug dump that leaks one.

### owner
`create` | `attach`, fixed at session creation. The single thing that decides
whether teardown **closes a browser**: `create` means the daemon launched it
and will close it; `attach` means someone else owns it and teardown leaves it
running. Only `cdp --create` is create-owned. `extension` and `cdp --attach`
are always `attach` — the user's Chrome and someone else's browser are never
ours to kill.

**Trap:** owner governs the *browser*, nothing else. An attach-owned extension
session still closes its own tab group, and the executor is reaped on every
backend regardless of owner.

### daemon
The single global process serving one **endpoint** — by default
`http://127.0.0.1:19990`. It serves all sessions at once. There is exactly one;
per-session daemon names are gone (see *Retired*).

**Trap:** "exactly one" is enforced by the endpoint's TCP bind, not by a file.
A second `serve` pings `/__ping__` first and refuses politely; if it races past
that, `EADDRINUSE` stops it. The control socket file that used to carry both
jobs is gone, and with it the watchdog that self-exited a daemon whose socket
was replaced — there is nothing left to replace.

### downstream
Everything that connects **into** the daemon: the CLI, the skill client, a
Playwright client on the cdp surface. Downstream must never branch on backend —
all backend divergence is absorbed inside the daemon.

### upstream
The daemon's connection **out toward the browser**. The mirror of downstream.
Spoken through the `Upstream` protocol (below) by one adapter per backend.

### Upstream (protocol)
The declared, session-shaped interface (`daemon/server/upstream.py`) every
backend adapter satisfies, and the only way anything outside the adapters
touches a browser. Two adapters:

| adapter | backend | owns |
|---|---|---|
| `CdpUpstream` (`upstream.py`) | `cdp` | one raw CDP websocket; the Chrome a `--create` session launched (`owns_browser`, `browser_pid`); the endpoint `cfg` |
| `ExtensionUpstream` (`extension_upstream.py`) | `extension` | the relay; extension hello/closed and Target events; the session→tab-group binding |

Members, grouped:

- **lifecycle** — `start`/`stop` (daemon-lifetime resources: the relay's
  listening socket) · `open`/`close` (one connection) · `attach`/`detach`
  (atomic publication to `Router`) · `is_open` · `relay` (or `None`)
- **readiness / recovery** — `bind_recovery` · `await_browser` and
  `converge(session, force)` (steps 1 and 3 of the **drivable path**) ·
  `reconnect` (rung 1 of `recover`)
- **tabs** — `open_tab` · `close_session_tab` · `list_tabs` · `get_targets` ·
  `target_belongs_to_session` · `current_page` · `attach_active`
- **teardown** — `end_session(session, deadline=None)`: the adapter applies
  the owner rule to the browser
- **wire / misc** — `send_cdp` · `wait_session_announce` ·
  `userscript_request` · `reload_extensions`

**Trap:** the adapter object is long-lived — built with its context, opened
and closed many times (lazy open, idle close). Browser ownership therefore sits
on the adapter, not on a connection: a `CdpUpstream` kills only a pid it
launched, on every close path, and an attach-owned one never has a pid.

**Trap:** never ask which adapter you hold (`isinstance`, `backend ==`,
`relay is None`) outside the adapters and `upstream_context.py`'s factory. If
a caller needs a backend difference, it is a missing protocol member.

### UpstreamContext
One bundle per live upstream (`daemon/server/upstream_context.py`):
`{ state, router, holder, upstream }`. `extension` sessions share the daemon's
one context; each `cdp` session gets its own, built lazily from its ledger
record by `context_for_record` — the one place a backend name maps to an
adapter class. A per-session context's connection *is* its session's
workspace, so `end_session` closes it and the daemon drops it.

The `holder` (`UpstreamHolder`) is the backend-agnostic part: lazy open, the
§6.5 close etiquette, and the state/publication transitions around the adapter.

**Trap:** the holder has no backend fields, on purpose. Launch/kill, relay
events and tab convergence belong to the adapter; putting one back on the
holder recreates the side-by-side lifecycle this split removed.

### relay
The websocket server (default port **19989**) that the unpacked Chrome
extension dials into. It is *not* a CDP server — it speaks a small app-level
protocol and turns requests into `chrome.debugger` calls.

**Trap:** the port clears playwriter's 19988 deliberately. Don't renumber it
without re-reading the comment in `daemon/cli.py`.

### executor
The resident per-session process that owns that session's **one and only**
Playwright controller. Requests run FIFO. Every browser-driving path — `-e`
code, CLI tasks, inline `run_task()`, userscript verification — reuses its live
`page` / `context`.

**Trap:** the request deadline is fail-stop. On expiry the daemon terminates
that exact executor and waits for confirmed process death. Tabs survive;
executor `state` does not, and `finally` blocks are not guaranteed.

**Trap:** its unix socket is **daemon-internal**. Clients reach it through the
endpoint's `/exec` relay, and `ensureExecutor` answers with readiness plus an
`executor_id`, never a path. A socket path is meaningless from another machine,
which is exactly what made remote use impossible before ADR-0011.

**Trap:** a replacement daemon adopts a live executor only when its discovery
record has a matching pid start-time fingerprint, socket, and executor id. A
real daemon stop still reaps it; request deadlines and session reset/end remain
fail-stop and deliberately lose executor `state`.

### recovery state
The daemon's persisted per-session answer to which layer is currently broken:
`healthy`, `extension-disconnected`, `tab-gone`, `executor-unbound`,
`executor-dead`, or `needs-human`. Relay, tab-recovery, and executor lifecycle
events are inputs to this state; `status`, `doctor`, and `browserwright recover`
read it. `since` is when the diagnosis last changed and `reason` is evidence,
not a remediation guess. ADR-0013.

The state machine (`session_state.RecoveryStateMachine`) is the only
authority. Every other hop *reports*: the relay (hello, closed, Target
detach), the executor registry (ready, exited, reaped), the drivable path's
`converge`, and the executor itself, through `ExecuteResponse.recovery_event`
— `bound` (a call completed on the held page), `rebound` (the tab died and
`page` was re-bound in place, before or during the call), `target-gone` (the
rebind failed; the executor recycles). The exec relay feeds that event to the
machine (`bound`/`rebound` → tab recovered, `target-gone` → tab lost) and
strips it from the agent's frame.

**Trap:** never infer recovery from the agent-facing response. An in-place
rebind answers the agent with an *error* ("RETRY the call") and is a
*recovery*; reading `error` / `terminal_reason` misfiled it as neither.

### drivable path
The one sequence that makes a session's browser side usable:
`Daemon.ensure_session_drivable(session, force=False)` = the adapter's
`await_browser` (bounded grace for the browser to be reachable) → the holder's
`ensure_open` → the adapter's `converge` (one live tab; `force` skips the
healthy fast path). `Daemon.ensure_executor` runs it inside the executor
registry's per-session lifecycle lock and then spawns the executor. Every
entry uses it: `ensureExecutor`, the `/exec` relay, `recoverSession`
(forced, no spawn — its caller is usually the executor binding), and
`recover` (rungs 2 and 3).

**Trap:** do not add a second copy "because this caller is usually warmed
up". The `/exec` relay's copy skipped `converge` whenever the upstream was
connected, and handed out executors for sessions whose tab was gone.

### endpoint
The daemon's **single TCP front door** (default `http://127.0.0.1:19990`) — the
only way any downstream reaches the daemon, local or remote (ADR-0011). One
server, three sub-surfaces, dispatched by ws path:

| surface | path | who speaks it |
|---|---|---|
| **cdp surface** | `/cdp` | Playwright / puppeteer `connect_over_cdp` |
| **control surface** | `/control` | the CLI and the skill client (`?session=` + `BrowserwrightDaemon.*` verbs) |
| **exec-relay surface** | `/exec` | the executor data plane, relayed by the daemon |

Plus HTTP: `/json/version`, `/json`, `/json/list` (CDP bootstrap) and
`/__ping__` (liveness). Anything else is a 4xx.

The **cdp surface** is what the retired term *facade* named. For `cdp` it is a
byte-for-byte passthrough; for `extension` it is a *synthesis* layer mapping
browser-level CDP concepts onto the session's tab group.

**Trap:** that synthesis exists only because the relay is not a native CDP
server. Never copy it into the raw-CDP paths.

**Trap — there is no authentication, and that is a decision, not an
oversight.** The endpoint grants arbitrary code execution; the security
boundary is entirely the network layer (loopback by default, a tailnet or SSH
tunnel for remote), plus Origin validation rejecting browser-originated ws
upgrades. Weighed against a `0600` token file and rejected knowingly — see
ADR-0011 before "fixing" it.

**Trap — the address is one URL, and configuring it explicitly changes
behavior.** `BW_DAEMON_URL` / `--daemon-url` / toml `daemon_url`, defaulting to
`http://127.0.0.1:19990`. An explicitly configured URL — *even localhost* —
means the client will never auto-start or restart that daemon: it is someone
else's process, possibly on another machine. Only the unconfigured default
keeps auto-start.

**Trap — an in-flight `/exec` call can still be severed by daemon replacement.**
The caller retries once the replacement is up. Between calls, the replacement
adopts fingerprint-verified executors, and the next call reconnects the
executor's Playwright controller while preserving its Python `state`.

### initiator
Who caused a daemon lifecycle event: `launchd` (parent pid 1, nothing
stamped), or `cli:<verb> cwd=… parent=…` for `restart` / `stop` / an
on-demand `serve` spawn (`_ipc.describe_initiator`, carried to a child
through `BW_DAEMON_INITIATOR` by `daemon_lifecycle.ensure`). ADR-0012 rule 5.

**Trap:** launchd relaunches the daemon after a CLI `restart`, so the new
daemon's own start line says `launchd`; the CLI's `LIFECYCLE restart` line
just before it is the attribution.

### daemon lifecycle
The client side's one owner of "is the daemon up, and make it so"
(`src/browserwright/daemon_lifecycle.py`). Three calls: `diagnose()` returns
one `DaemonVerdict` (`up` · `stale` · `down` · `foreign` · `unreachable` ·
`undecided`), `ensure(reason)` is the **only** Layer 2 code that starts or
replaces the daemon (executor handoff, initiator, `LIFECYCLE` line, child env),
and `unreachable_fix()` builds the agent-facing diagnosis text. Every
`browserwright-daemon <verb>` a Layer 2 module runs goes through its
`run_verb` adapter, which carries the resolved endpoint into the child.

**Trap:** constructing a `Session` (or its `ModeBClient`) has no lifecycle side
effect — no probe, no version check, no restart. Version coherence is enforced
by `ensure()` at `session new` / `recover` (and the session verbs that already
talk to the daemon CLI), never by opening a connection. An explicitly
configured endpoint (see `endpoint`) is diagnosed, never started or replaced.

**Trap:** `diagnose(confirm=True)` takes two probes and concludes nothing when
they disagree (`undecided`); only `down` and `stale` on the default endpoint
are `replaceable`. `foreign` (something that is not browserwright holds the
port) is never replaced by `recover` — ADR-0013 rule 2.

### activity gate
The daemon's own answer to "would interrupting me hurt someone right now":
in-flight relay calls, executors running code, pending router requests, and
sessions touched within a window (`restart_guard.probe`, surfaced as
`browserwright-daemon activity`, exit 4 = busy). `restart`, `upgrade-global`
and the e2e runner all consult it and refuse while it says busy;
`restart --force` is the only override, for humans. ADR-0012 rules 2 and 4.

### lifecycle event
A daemon start / stop / restart / spawn, written as one `LIFECYCLE <event>`
line into the daemon log (`_ipc.log_lifecycle`) by whichever process caused
it, so the record exists even when the daemon being replaced never logs its
exit. Distinct from the extension's `hello` / relay reconnects, which are
connection events, not process events.

### Router
The frame-routing engine (`daemon/server/proxy.py`). Owns request-id rewriting,
local↔upstream sessionId translation, the single-attacher rule, and the
pre-open frame buffer. Its state lives in `DaemonState` (`state.py`).

### view
A **per-heredoc injected, read-only** function that renders the session's
current `page` into text for the agent. Two members today:

| view | answers | used for |
|---|---|---|
| `snapshot()` | what can I **do** here | an a11y tree; every actionable node carries `[ref=eN]` |
| `read_markdown()` | what does it **say** | the page as Markdown, links absolute |

"Read-only" is the class invariant, not a coincidence: a view never navigates,
never opens a tab, never mutates the page. That is why `read_markdown()` takes
no `url` — navigating would move the working tab and invalidate every `[ref=eN]`
the agent is holding, from a call that reads like a read. See
[ADR-0006](docs/adr/0006-markdown-is-the-content-view.md).

**Trap — a view can never be in `EXPORTS`.** `EXPORTS` holds module-level
functions, which cannot have a live `page`. Views are built by a
`make_*(handle)` factory in `repl/` and injected by
`repl/_namespace.build_globals`. Consequently **`--print-skill`'s generated
section cannot list them** (`skill_doc.py` walks `EXPORTS`), so a view only
exists to the agent if `skill_runtime.md` says so in prose. Adding a third view
without editing that file ships it invisible.

**Trap — every view is bound TWICE, and the second binding is easy to skip.**
`build_globals` binds each `make_*(handle)` against a *lazy* `PlaywrightHandle`,
which is correct only in-process. The resident **executor** already owns a live
driver on its worker thread, so it re-binds the whole surface through
`_Worker._bind_live_surface` against its shared `_LivePageHolder`. Miss that
second binding and the view resolves the lazy handle inside a running asyncio
loop — `Playwright Sync API inside the asyncio loop`, on every call, on every
page. That is issue #59, and it is why the executor's rebind list must grow
whenever `build_globals` binds a new name to `handle`.

**Trap — the word is also loose English elsewhere.** "the ledger view", "the
in-process view", "the relay's ghost view" in various docstrings predate this
entry and mean nothing in particular. Only the injected-function sense is the
defined term.

### verb
A `BrowserwrightDaemon.*` JSON-RPC method the daemon answers itself rather than
forwarding upstream — `openBackgroundTab`, `closeTab`, `endSession`,
`ensureExecutor`, `attachActiveTab`, `recoverSession`, `userscript.*`, …

**Contract:** every verb returns a **same-shape, honest** result on every
backend. Where a concept is backend-specific the daemon falls back to the
nearest honest equivalent — never a fabricated value, and **never `-32601`**.
"Uniform shape" is required; "identical meaning" is not.

### binding
The link from a session to its live browser handle: the numeric `group_id` on
`extension`, the attached target on `cdp`. Live binding lives in
process; the ledger holds the durable copy used to recover after a restart.

**Extension anchor (ADR-0009):** the group **title** is the binding, and the
only one. It is `<name>-BW<sid>` — the session's `--name`, then a visible `-BW`
token, then the session id. The extension writes it when it creates the group
and finds the group again by comparing titles exactly; the numeric groupId is
Chrome's handle, useful within one browser session and never an identity.

Three properties do the work, and each is load-bearing:

- **Ours, and it survives.** We write the title; Chrome restores it with the
  group. That makes it the only anchor that is both. A browser restart is the
  only situation in which a stale group can still exist — Chrome recycles
  groupIds across one, and the per-tab markers this replaced lived in
  `chrome.storage.session`, which Chrome wipes on one by design.
- **`-BW` is a namespace, not decoration.** It is what makes a title match
  structurally unable to land on a group the *user* created. That is the
  failure direction #29 existed to prevent, and here it is closed by
  construction rather than by probability.
- **`sid` makes it unique without entropy.** The ledger's `next_id` is
  monotonic and never reused, so two sessions cannot collide.

**Trap — the assumption is that titles don't change.** A user who renames the
group takes it out of the session by that act: we no longer find it, treat it
as gone, and neither warn nor retry. That is accepted, not overlooked — it buys
exactly one lookup path. #29 was itself forced by two heuristics disagreeing,
and a second anchor would be that trap a third time.

### ghost target
A synthesized CDP `targetInfo` for an extension tab. The extension backend has
no real CDP targets, so the relay fabricates them (`make_target_info`) for both
the control surface and the cdp surface.

### Layer 1 / Layer 2
Layer 1 = `src/browserwright/daemon/` — the daemon, backends, relay, endpoint.
Layer 2 = the rest of `src/browserwright/` — the agent CLI, sessions,
primitives, site skills, memory.

**Trap:** raw CDP belongs to Layer 1. If Layer 2 code is opening a websocket to
Chrome, it is either a test (mock it) or a mistake (don't).

---

## Being introduced

Named here so parallel agents use the same word. It does not exist in code
yet — check before you reference it.

### in-flight registry
One place holding every in-flight request with a start time, readable through a
`status` verb and `browserwright-daemon ps`. Today each hop keeps a private
table with no timestamp, so a hung daemon is indistinguishable from an idle one.

---

## Retired — do not bring these back

| Term | Status |
|---|---|
| `BD_NAME` / `--name` as a *daemon* name | Gone. Daemon isolation is the endpoint port (`--facade-port` + `BW_DAEMON_URL`). |
| `facade` | Retired into **endpoint / cdp surface** (ADR-0011). The `--facade-port` / `--facade-host` / `BD_FACADE_*` / `facade_port` knobs keep their names as the *endpoint's* bind config. |
| the unix control socket (`browserwright-daemon.sock`) | Deleted with the facade. One TCP endpoint, `/control` path. Its `--facade-port 0` "disable" value is gone too: `0` now means an ephemeral port. |
| `ensureExecutor` returning `exec_sock` | Gone. It returns `{ready, executor_id}`; the data plane is the endpoint's `/exec` relay. |
| `--name` as an identity key | It is a human label only. Use session id, or `group_id` for extension recovery. |
| backend fields on the upstream holder (`holder.relay`, `holder.cdp_pid`, `cdp_owns_browser`, `_extension_adapter`, `make_context`) | Moved into the adapters and `upstream_context.py`. The holder is backend-agnostic; see *UpstreamContext*. |
| `end_session_before` / `teardown_cdp_context` | Collapsed into `Upstream.end_session(session, deadline=None)` behind `Daemon.end_workspace`. |
| `_owned` / `_borrowed` tab sets | Being deleted — group membership (`chrome.tabs.query({groupId})`) is the single source of truth. |
| Querying a tab group by title | Gone from `background.js`. Titles are user-editable and not unique; key on numeric `groupId`. |
| `backend == "rdp"` as "speaks raw CDP" | Use `_raw_cdp_backend` (`!= "extension"`). A name check excluded `env` for a whole release. |
| `rdp` / `env` as backend values | Both are `cdp` (#38). `--remote-debugging-port` named a launch flag, not the protocol — and a cloud browser hands you a URL, never a port. |
| `BD_CDP_WS` / `BD_CDP_URL` / `BU_*` | Gone. A CDP endpoint is per-session ledger state (`workspace.url`), not process-global. |
| one `env` session per daemon socket | Gone with `daemon_scope`. One daemon holds N attached browsers. |
