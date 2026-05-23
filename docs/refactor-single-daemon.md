# Refactor: single global daemon, session-keyed upstream multiplexing

Branch: `refactor/single-global-daemon`

## Motivation

The old model keyed daemon identity on `BD_NAME` (default `"default"`) and made
one daemon serve exactly one backend. rdp sessions each spawned their own daemon
(`browserwright-daemon-s{id}`); extension sessions shared the `default` daemon.
This produced confusing failures (`no Mode B endpoint for BD_NAME='default'`
when the named daemon wasn't up) and an opaque `default` that is neither a
backend nor meaningful to users.

## Target model (locked with user)

1. **One global daemon. Fixed endpoint, no name.** Socket
   `${XDG_RUNTIME_DIR:-/tmp}/browserwright-daemon.sock` (Windows: `.port`).
   `BD_NAME` is **deleted** entirely — env var, `--name` flag, `cfg.name`, the
   `-{name}` socket suffix, and the `daemon_endpoint` ledger field all go.
2. **session ↔ backend is fixed at creation and immutable for the session's
   whole life.** Source of truth = the ledger `backend` field, written once by
   `session new`, never mutated. No "switch backend" path exists anywhere.
3. **The one daemon serves BOTH backends simultaneously**, routing per session:
   - extension sessions all share ONE relay-backed upstream (the user's real
     Chrome via the relay on 19989).
   - each rdp session gets its OWN upstream — **the daemon itself launches and
     owns** the per-session Chrome (independent port + profile `bs-s{id}`), and
     tears it down on session end.
4. **A skill client connects with `?session=<id>` only** (no backend). The
   daemon looks up the session's backend from the ledger and routes the client
   to the right upstream. The client is fully unaware of multiplexing — it sees
   a vanilla single-browser CDP stream.

## Key insight: the engine already exists

`server/proxy.py` (`Router`) + `server/state.py` (`DaemonState`) are **already a
complete single-upstream / multi-client engine**: id remap, sessionId local↔
upstream translation, single-attacher rule, shared-read readers — all operate
only through `self.state.*` tables and a single `self._upstream_send`. There is
no hidden global coupling. So the refactor is **instantiate this engine
per-upstream + add a dispatcher**, NOT a rewrite.

`server/listener.py`'s `_UpstreamHolder` is the per-upstream lifecycle owner
(lazy-open, close etiquette, idle, extension-callback wiring). It maps 1:1 to a
context too.

## Decomposition

```
UpstreamContext            # one per live upstream
  ├─ state:   DaemonState  # per-upstream tables (phase, ws_url, upstream_to_locals,
  │                        #   attachers, pending_requests, _next_upstream_id, targets,
  │                        #   clients belonging to THIS context)
  ├─ router:  Router       # the existing proxy, unchanged logic
  └─ holder:  _UpstreamHolder  # lifecycle; extension-only callbacks only on relay ctx

Daemon (thin, global)
  ├─ relay_context: UpstreamContext      # shared by ALL extension sessions; relay always-on
  ├─ contexts: dict[session_id, UpstreamContext]   # one per rdp session
  ├─ _next_client_id: count              # global, unique across contexts (log-friendly)
  └─ dispatcher: on client connect (?session=<id>) → read ledger backend →
                 get/create context (rdp: launch Chrome) → hand client to ctx.router
```

Routing needs no per-frame inspection: **client identity → its one session →
its one context → its one upstream**. Cross-talk is structurally impossible.
`Router._broadcast` is scoped to the context's own clients (browser-level events
only belong to that upstream).

## Unified downstream interface (locked with user)

**Regardless of whether the upstream is extension or rdp, the downstream sees ONE
identical interface.** The skill client / code agent must never branch on the
backend. All backend divergence is absorbed inside the daemon, dispatched by the
context's (immutable) backend.

Consequence: the currently extension-only RPCs become **backend-neutral session
verbs**, each gaining an rdp implementation, exposed identically. The skill drops
all `backend_name` branching (`Session.backend_name` stays for diagnostics only).

| Unified verb | extension impl | rdp impl |
|---|---|---|
| `ensureSession` | attach client to shared `relay_context` | create context, launch Chrome, open upstream |
| `openTab(url)` (was `openBackgroundTab`) | `ext.open_background_tab` (durable tab group) | `Target.createTarget` + attach |
| `closeTab` | `ext.close_tab` | `Target.closeTarget` |
| `endSession` | close the whole session group — ALL member tabs (DECIDED) | close the whole owned Chrome + drop context |
| `attach_active` (kept as a first-class verb) | adopt the user's focused tab **into this session's group** (see C1) | fallback to `current_page` — the daemon-owned Chrome's current front tab |
| `recoverSession` | rebuild from durable tab group | re-attach to surviving targets, else relaunch |
| `userscript.*` | extension impl | rdp impl via `Page.addScriptToEvaluateOnNewDocument` (or document as N/A with a uniform, non-`-32601` answer) |

Rule (revised, locked with user): every verb returns a **same-shape, honest**
result on every backend. Where a concept is backend-specific, the daemon falls
back to the **nearest honest equivalent** — never a fabricated value — and the
divergence is documented, not hidden, and never surfaced as `-32601`. "Uniform
shape" is required; "identical meaning" is not. A fallback that *lies* (returns
something that doesn't truthfully describe the backend) is forbidden.

## Downstream API — the contract (DECIDE HERE; annotate inline)

Three tiers. Tier A is already uniform and unchanged. Tier B is the work. Tier C
is what I do NOT think can be made *semantically* identical — flagged for you.

### Tier A — already backend-agnostic (no change)

These operate on the session's attached CDP target and behave identically on
extension and rdp today. They stay as-is:

`goto_url` · `reload` · `wait` · `wait_for_load` · `js` · `click_at_xy` ·
`type_text` · `press_key` · `scroll` · `fill_input` · `dispatch_key` ·
`upload_file` · `wait_for_element` · `wait_for_network_idle` · `drain_events` ·
`capture_screenshot` · `snapshot` · `diff_snapshot` · `describe_page` ·
`page_info` · `cdp` · `iframe_target` · `http_get` · `switch_tab` ·
`attach_readonly` · site/memory (`remember*`, `bootstrap_site`, `memory_read`) ·
discovery (`list_site_skills`, `load_site_skill`, `run_task`).

### Tier B — tab lifecycle: UNIFY (daemon absorbs the backend split)

Today these branch on `sess.backend_name` or return `-32601`. Proposed unified
surface — same name, same return shape, daemon dispatches by context backend:

| Verb (downstream) | Unified semantics | extension impl | rdp impl |
|---|---|---|---|
| `open(url, *, background=True)` | open a new working tab for THIS session, attach, bind as current. Returns `{targetId,tabId,url,title}`. **Replaces both `new_tab` and `open_background`.** | `openBackgroundTab` (tab group = session name) | `Target.createTarget` |
| `close_tab(target_id \| session_id)` | close one of the session's tabs; invalidate its session | `chrome.tabs.remove` | `Target.closeTarget` |
| `list_tabs()` | the session's tabs `[{targetId,url,title,attached}]` | attached ghost targets in group | `Target.getTargets` |
| `current_page()` | the session's current working tab; auto-open if none | cached → recover → `open()` a new tab (NOT adopt — DECIDED) | cached → first target → create |
| `current_tab()` | current binding or `None` | same | same |
| `ensure_real_tab()` | switch off internal pages to a real one | same | same |

#### The extension "browser" = the session's tab group (locked with user)

The unifying model: **a session IS a logical browser.** On rdp that browser is a
dedicated Chrome process (own profile). On extension it is **one Chrome tab
group, owned exclusively by that session** — session ↔ group is 1:1, and the
group name is the session name. This is the extension analogue of rdp's dedicated
Chrome, and it makes the group the durable, user-visible, restart-surviving
identity of the session's browser.

Three invariants make this real (current code does NOT yet enforce them — they
are work):

- **Bind to `groupId`, not the title.** On session create, create the group,
  capture its `groupId`, and persist it (session state / ledger). All later
  operations key on `groupId`. The title (= session name) is used *only* as a
  recovery anchor when the `groupId` is lost across a daemon restart. Reason: a
  user can rename a group, and Chrome allows duplicate group titles — keying on
  title (today's `chrome.tabGroups.query({title})`) is unstable and can collide
  across sessions.
- **The group's live membership is the single source of truth** for what is "in
  this session's browser." `list_tabs` / `Target.getTargets` (extension branch) /
  `current_page` resolve from `chrome.tabs.query({groupId})`, **not** the
  in-memory `_owned`/`_borrowed` sets — those miss tabs the user dragged in,
  popups, and tabs recovered after a restart.
- **Entering/leaving the group = entering/leaving the session's browser.** A tab
  dragged out of the group leaves the session (daemon detaches + drops it via
  tab/group events); a tab the user opens *outside* the group is invisible to the
  session. Once enumeration is scoped to `groupId`, sessions are mutually
  invisible even though they share one Chrome (storage is still shared — see C4).

**Consequence — no `_owned`/`_borrowed` tracking (DECIDED `endSession` = close
whole group).** Because endSession closes every member of the group and
enumeration is already groupId membership, the daemon does not need the
owned-vs-borrowed distinction at all. Delete the `_owned`/`_borrowed` sets.
"Don't want a tab closed when the session ends? Drag it out of the session's
group first." adopt simplifies to "move the tab into the group" — it closes with
the group like any other member.

Proposed decisions baked in (override inline if you disagree):
- **Collapse `new_tab` + `open_background` → `open(url, background=True)`.** Keep
  `open_background`/`new_tab` as thin deprecated aliases for one release.
- **`background=` is honored only on extension** (real user focus to protect);
  on rdp it's a no-op (no user, every tab is "background").
- **`group=` drops from the public signature.** It was the extension's durable
  reconnect anchor — make it an internal detail: the daemon derives the group
  from the session (name → group on create, `groupId` thereafter), the downstream
  never passes it. (See the tab-group model above and C2.)
- **Drop the `NeedsUserConfirm` "zero attached tabs" raise** from `list_tabs`/
  `current_tab`. It encodes an extension-only mental model. Unified behavior:
  `current_page()` just opens a working tab; `list_tabs()` returns `[]`.
- **`current_page()` empty fallback = `open()` a new tab, NOT adopt (DECIDED).**
  Adopt moves the user's focused tab into the group — too invasive for an
  implicit "get current page" call. Only the explicit `attach_active()` adopts.
- **`endSession` closes the whole session group (DECIDED).** No owned/borrowed
  distinction (see the consequence note above).

### Tier C — CANNOT be made semantically identical (your call)

These are where "unify" can only mean "uniform shape / never `-32601`", NOT
"same meaning". I need your decision on each.

- **C1. `attach_active` — DECIDED (locked with user): keep as a first-class
  verb, `adopt` semantics on extension, fallback to `current_page` on rdp.**
  - **extension = adopt** (replaces the old "borrow" behavior): find the user's
    focused tab → `chrome.tabs.group({groupId, tabIds:[t]})` to **move it into
    this session's group** → attach the debugger. The tab is now a group member;
    it closes with the group on `endSession` like any other member (no separate
    "owned" flag — see the consequence note in Tier B).
    - **Conflict rule (DECIDED): refuse.** If the focused tab already belongs to
      *another* session's group, `attach_active` returns an error and does NOT
      steal it. (We do not move it out of the other session's group.)
  - **rdp = fallback to `current_page`**: the daemon owns the Chrome, so "the
    page currently shown" is the session's current front tab (no human contends
    for focus). CDP has no first-class "which target is active", so define it as
    the session's current bound target (multi-window: most-recently-fronted);
    create+attach if none. This is an honest equivalent, not a fabricated value,
    so it satisfies the revised Rule above.

- **C2. Cross-restart durability / `recoverSession` — DECIDED: rdp is ephemeral
  for v1.** rdp's Chrome is a daemon child and dies with the daemon. `recover`
  on rdp is a uniform no-op (returns live tabs, or relaunches empty — state
  lost). On daemon startup, clean up orphaned rdp Chrome processes / `bs-s{id}`
  profiles left by a prior crash. Detached-survivable rdp (launch detached +
  reconnect by port) is explicitly deferred past v1. Extension recovery (rebuild
  from the durable tab group via title→groupId) is unchanged.

- **C3. `userscript.*` — proceeding with the rdp shim** (no objection raised).
  rdp implements via `Page.addScriptToEvaluateOnNewDocument`; caveats (MAIN-world
  vs the extension's isolated world, match-pattern differences) documented. If
  you'd rather defer to a uniform honest "not-supported-on-rdp" answer, say so.

- **C4. Storage/cookie isolation is NOT uniform — and no fallback can smooth it
  (it's an ambient property, not a verb).** rdp gives each session a dedicated
  Chrome profile → isolated cookies/localStorage. The extension's tab group
  isolates only the *tab set*, not storage: all extension sessions live in the
  user's one profile and **share cookies / origin-keyed storage / login state**
  with each other and with the user. This is intentional (the whole point of the
  extension backend is to reuse the user's real logged-in session), but it must
  be stated in the contract: downstream must not assume two extension sessions
  are isolated the way two rdp sessions are. Two extension sessions hitting the
  same origin share that origin's cookies; two rdp sessions do not.

## RPCs

- `BrowserwrightDaemon.ensureSession {session_id}` — backend read from ledger
  (NOT a param). Idempotent. extension → attach client to `relay_context`;
  rdp → create context, launch Chrome, open `UpstreamConnection`. Rejects if the
  session's backend somehow changed (immutability guard).
- `BrowserwrightDaemon.endSession {session_id}` — rdp: close that session's
  Chrome + upstream + drop the context. extension: existing per-session tab
  teardown (close owned tabs, keep borrowed).
- The session verbs above are dispatched per-context; the proxy layer routes to
  the extension callback or the rdp CDP implementation based on the context's
  backend, so the wire-facing method names and result shapes are identical.

## Phases (single coherent change on the branch; suite goes red mid-way because
denaming breaks the old per-session-rdp-daemon model until Phase 3 lands)

- **P1 — denaming.** `_ipc.py`/`mode_b_client.py` fixed paths, drop `name`
  params; `config.py` drop `Config.name`/`BD_NAME`/`cli_name`/`check_name`-for-name
  (keep regex validator for `--profile`); `cli.py` drop `--name` from all 13
  subcommands + the `glob(browserwright-daemon-*.sock)` cleanup; `install.py`
  drop `--name`; `active_tab.py`/`listener.py` callers.
- **P2 — multi-upstream + session routing.** Introduce `UpstreamContext`; make
  `Daemon` hold `contexts` + `relay_context`; dispatcher in `_ClientHandler`;
  `?session=` parsing; relay always-on; `serve` drops the backend requirement;
  `ensureSession`/`endSession`; scope `_broadcast`; per-context idle/status/stats.
- **P3 — rdp Chrome owned by daemon.** `ensureSession(rdp)` launches Chrome
  (absorbs `session_create._launch_daemon` / `launch-chrome`), tracks PID,
  `endSession` kills it. `session_create.py` drops `_launch_daemon`/
  `_rdp_endpoint`/`_shared_extension_endpoint`: just ensure the one daemon is up,
  then `ensureSession`.
- **P4 — ledger cleanup.** `session_registry.allocate` drops `daemon_endpoint`;
  update `cli.py` session-list display + whoami.
- **P5 — tests + e2e isolation + docs.** ~35 test sites drop `BD_NAME`/`--name`/
  `daemon_endpoint`. e2e isolation moves from `BD_NAME` to `XDG_RUNTIME_DIR`
  (socket dir) + relay-port override (`BD_EXTENSION_PORT`, 29989). Update
  `daemon.md`, `session-model.md`, `skill.md`, `README.md`, `SKILL.md`.

## Notes / invariants to preserve

- Keep the `[A-Za-z0-9_-]{1,64}` regex validator — it still guards
  `launch-chrome --profile` (`launch_chrome.py:58` via `config.check_name`).
- The pre-open buffer race fix (Task #76), single-attacher rule, shared-read,
  and lifecycle event broadcasts (`upstreamConnecting`/`upstreamReady`/
  `upstreamClosed`) must keep working per-context.
- `_on_upstream_closed` for an rdp context should drop that context (its Chrome
  is gone), not just mark disconnected.
- Unified interface adds work: P2/P3 must implement the **rdp counterparts** of
  the session verbs (openTab/closeTab/endSession/attach/recover) so none returns
  `-32601`; P5 removes `backend_name` branching from the skill primitives.
- **extension tab-group invariants (new work):** bind the session to a captured
  `groupId` (persisted), key all ops on `groupId`, use the title only as the
  restart recovery anchor; make `chrome.tabs.query({groupId})` the source of
  truth for membership (not `_owned`/`_borrowed`); wire tab/group events so a tab
  dragged out of the group is detached + dropped from the session. This replaces
  today's `chrome.tabGroups.query({title})`-by-title lookup (`background.js`).
- **`attach_active` adopt + refuse-on-conflict:** adopting moves the user's tab
  into the session's group (it then closes with the group on `endSession`) — a
  change from today's borrow semantics; refuse (error, no steal) when the tab
  already belongs to another session's group.
- **Empty groups auto-delete (Chrome behavior):** a group with zero tabs is
  removed by Chrome and its `groupId` goes invalid. So a session that closed its
  last tab has no live group; the next `open()` recreates the group from the
  session name. This is why the title is kept as the recovery anchor.
- **rdp orphan cleanup (C2 ephemeral):** on daemon startup, kill stray Chrome
  processes / remove `bs-s{id}` profile dirs from a prior daemon crash before
  serving, so ephemeral rdp sessions start clean.
- **Chrome-extension `background.js` is in scope:** the groupId-binding +
  membership-as-truth + adopt + drag-out-detach work lands in `background.js`,
  replacing today's `chrome.tabGroups.query({title})` title lookup. Fold into
  P2/P3's extension implementation (not a separate phase, but new surface area
  beyond the Python daemon).
