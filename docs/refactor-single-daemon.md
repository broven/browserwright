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
| `endSession` | close owned tabs, keep borrowed | close the whole owned Chrome + drop context |
| attach working target (was `attachActiveTab`) | `ext.attach_active_tab` (popup/active pick) | first/new target — daemon owns Chrome, no user-active ambiguity |
| `recoverSession` | rebuild from durable tab group | re-attach to surviving targets, else relaunch |
| `userscript.*` | extension impl | rdp impl via `Page.addScriptToEvaluateOnNewDocument` (or document as N/A with a uniform, non-`-32601` answer) |

Rule: a verb must never surface `-32601` ("requires the extension backend") to
the downstream. Either it has an rdp implementation, or it returns a uniform
documented result that is identical in shape across backends.

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
| `current_page()` | the session's current working tab; auto-open/attach if none | cached → recover → (see C1) | cached → first target → create |
| `current_tab()` | current binding or `None` | same | same |
| `ensure_real_tab()` | switch off internal pages to a real one | same | same |

Proposed decisions baked in (override inline if you disagree):
- **Collapse `new_tab` + `open_background` → `open(url, background=True)`.** Keep
  `open_background`/`new_tab` as thin deprecated aliases for one release.
- **`background=` is honored only on extension** (real user focus to protect);
  on rdp it's a no-op (no user, every tab is "background").
- **`group=` drops from the public signature.** It was the extension's durable
  reconnect anchor — make it an internal detail (daemon uses session name),
  not something the downstream passes. (See C2.)
- **Drop the `NeedsUserConfirm` "zero attached tabs" raise** from `list_tabs`/
  `current_tab`. It encodes an extension-only mental model. Unified behavior:
  `current_page()` just opens a working tab; `list_tabs()` returns `[]`.

### Tier C — CANNOT be made semantically identical (your call)

These are where "unify" can only mean "uniform shape / never `-32601`", NOT
"same meaning". I need your decision on each.

- **C1. "Attach the user's currently-focused tab" (`attach_active`).** Only
  meaningful on extension — rdp owns an isolated Chrome with no user and no
  foreground tab. Options:
  - (a) Keep `attach_active()` in the surface; on rdp it degrades to
    `current_page()` (returns the session's working tab). Uniform shape, silent
    semantic difference.
  - (b) Drop `attach_active()` as a public verb; fold "prefer the user's focused
    tab" into `current_page()` as an extension-only nuance. One fewer verb.
  - (c) Keep it extension-only but return a uniform, documented "not applicable
    on this backend" result (NOT `-32601`) on rdp.
  My lean: **(b)** — fewest concepts downstream.

- **C2. Cross-restart durability / `recoverSession`.** Extension recovers a
  session's tabs after a daemon restart because they live in the user's
  persistent Chrome (tab group survives). rdp's Chrome is a child the daemon
  launched — if the daemon dies, does the Chrome (and its tabs/cookies) survive?
  - If rdp Chrome dies with the daemon → "recover" can only relaunch a FRESH
    Chrome (state lost). That's a real semantic gap.
  - Decision needed: do we want rdp sessions to survive daemon restarts at all
    (launch Chrome detached + reconnect by port), or accept that rdp = ephemeral
    and `recover` is extension-only / a no-op on rdp?
  My lean: **rdp is ephemeral for v1**; `recover` is a uniform no-op on rdp
  (returns the live tabs, or relaunches empty). Revisit detached-Chrome later.

- **C3. `userscript.*`.** Extension injects via the extension's content-script
  machinery (persists across navigations, isolated world). rdp equivalent is
  `Page.addScriptToEvaluateOnNewDocument`. Close but not identical (isolated-world
  + match patterns differ). Decision: implement an rdp shim with documented
  caveats, or mark uniform-but-degraded?
  My lean: **rdp shim via `addScriptToEvaluateOnNewDocument`**, caveats in docs.

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
