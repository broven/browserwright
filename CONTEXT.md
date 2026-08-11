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
 │  ledger ──► session ──► UpstreamContext { state · Router · holder }   │
 │                                    │                                  │
 │  executor (one per session)        │ upstream                         │
 │  facade  (Playwright's door)       │                                  │
 └────────────────────────────────────┼──────────────────────────────────┘
                                      ▼
                        extension relay ──► user's Chrome
                        or raw CDP ws  ──► daemon-owned / external Chrome
```

---

## Core terms

### session
The unit of isolation, and the only durable identity. One code agent gets one
session. The session id travels through the Layer 2 CLI, daemon IPC, the
Playwright facade, and the ledger.

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
It is the **only** thing here that outlives every process: daemons restart,
executors are fail-stopped, Chrome closes, and each agent command is a fresh
shell — the session survives all of it because the ledger does.

A record has two tiers, and conflating them is the recurring bug:

| tier | fields | authority |
|---|---|---|
| durable fact | `id` · `backend` · `owner` · `workspace` · `name` · `created_at` · `last_seen` | authoritative — the daemon obeys these |
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
The single global process listening on
`${XDG_RUNTIME_DIR:-/tmp}/browserwright-daemon.sock`. It serves all sessions at
once. There is exactly one; per-session daemon names are gone (see *Retired*).

### downstream
Everything that connects **into** the daemon: the CLI, the skill client, a
Playwright client on the facade. Downstream must never branch on backend —
all backend divergence is absorbed inside the daemon.

### upstream
The daemon's connection **out toward the browser**. The mirror of downstream.

Two implementations exist today, playing the same role:

- `UpstreamConnection` (`daemon/server/upstream.py`) — a raw websocket to a real
  browser-level CDP endpoint. Used by `cdp`.
- `ExtensionUpstream` (`daemon/server/extension_upstream.py`) — the relay plus
  the Chrome extension's `chrome.debugger`, adapted to look like the above.

**Trap:** that "same role" is currently only a docstring claim. There is no
declared interface — `Router` is wired to whichever one by assigning twelve
mutable attributes in a required order. See *Being introduced* below.

### UpstreamContext
One bundle per live upstream: `{ state, router, holder }`. `extension` sessions
share the daemon's one context; each `cdp` session gets its own,
created lazily from its ledger record.

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

### facade
The CDP-speaking server (default port **19990**) that Playwright's
`connect_over_cdp` connects to. For `cdp` it is a byte-for-byte
passthrough. For `extension` it is a *synthesis* layer that maps browser-level
CDP concepts onto the session's tab group.

**Trap:** that synthesis exists only because the relay is not a native CDP
server. Never copy it into the raw-CDP paths.

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
the agent path and the facade.

### Layer 1 / Layer 2
Layer 1 = `src/browserwright/daemon/` — the daemon, backends, relay, facade.
Layer 2 = the rest of `src/browserwright/` — the agent CLI, sessions,
primitives, site skills, memory.

**Trap:** raw CDP belongs to Layer 1. If Layer 2 code is opening a websocket to
Chrome, it is either a test (mock it) or a mistake (don't).

---

## Being introduced

Named here so nine parallel agents use the same word. Neither exists in code
yet — check before you reference them.

### Upstream (protocol)
The declared interface both upstream implementations will satisfy, replacing
`Router`'s twelve mutable callback slots. Session-shaped, not transport-shaped:
`open_tab` · `close_tab` · `list_tabs` · `current_page` · `attach_active` ·
`end_session` · `recover` · `send_cdp`. Its two adapters are
`ExtensionUpstream` and `CdpUpstream`.

The adapter also becomes the **owner of the live binding** — the tab group is an
implementation detail of `ExtensionUpstream`; `cdp` has no such concept.

### in-flight registry
One place holding every in-flight request with a start time, readable through a
`status` verb and `browserwright-daemon ps`. Today each hop keeps a private
table with no timestamp, so a hung daemon is indistinguishable from an idle one.

---

## Retired — do not bring these back

| Term | Status |
|---|---|
| `BD_NAME` / `--name` as a *daemon* name | Gone. Daemon isolation is `XDG_RUNTIME_DIR` (distinct socket dir). |
| `--name` as an identity key | It is a human label only. Use session id, or `group_id` for extension recovery. |
| `_owned` / `_borrowed` tab sets | Being deleted — group membership (`chrome.tabs.query({groupId})`) is the single source of truth. |
| Querying a tab group by title | Gone from `background.js`. Titles are user-editable and not unique; key on numeric `groupId`. |
| `backend == "rdp"` as "speaks raw CDP" | Use `_raw_cdp_backend` (`!= "extension"`). A name check excluded `env` for a whole release. |
| `rdp` / `env` as backend values | Both are `cdp` (#38). `--remote-debugging-port` named a launch flag, not the protocol — and a cloud browser hands you a URL, never a port. |
| `BD_CDP_WS` / `BD_CDP_URL` / `BU_*` | Gone. A CDP endpoint is per-session ledger state (`workspace.url`), not process-global. |
| one `env` session per daemon socket | Gone with `daemon_scope`. One daemon holds N attached browsers. |
