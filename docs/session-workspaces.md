# Session Workspace Architecture

This document is the short, load-bearing reference for browserwright's session
workspace model. Read it before changing session routing, backend selection,
tab creation, Playwright facade behavior, or teardown semantics.

## Core Model

A browserwright session is the browser workspace assigned to one code agent. The
session id is the isolation key that travels through the Layer 2 CLI, daemon
IPC, Playwright facade, and ledger. A session's backend is chosen at
`browserwright session new` time and is immutable for the life of that session.

There is one global daemon on the fixed `browserwright-daemon.sock` socket. The
daemon serves multiple sessions at once and routes each explicit session by
reading the ledger record for that session. Do not reintroduce per-session
daemon names, `BD_NAME`, or backend selection based on client environment once a
session already exists.

The word "workspace" is backend-specific:

| Backend | Session workspace | Isolation boundary | Tab group usage |
|---|---|---|---|
| `extension` | One Chrome tab group inside the user's real Chrome | The set of tabs in that group only | Required: exactly one group per session |
| `rdp` create | One daemon-owned Chrome instance/profile for that session | The browser instance/profile | Never create or simulate tab groups |
| `rdp` attach | One externally-owned browser instance exposed on the recorded port | That browser instance | Never create or simulate tab groups |
| `env` | The externally-owned browser the daemon resolved (BD_CDP_WS / BD_CDP_URL) | That browser instance | Never create or simulate tab groups |

## Executor Ownership

Each session has at most one resident executor process, and that executor is
the session's only Playwright controller. Browser-driving `-e` code, CLI tasks,
inline `run_task()`, and userscript verification must reuse its live
`page`/`context` instead of opening a second Playwright facade connection.
Requests are FIFO; there is no second executor running concurrently inside one
session. Explicit `context.new_page()` remains the intentional way for that one
controller to create another tab.

Cold binding has one authoritative target: the target resolved and persisted by
the agent/session path. Playwright may need time to materialize its matching
`Page`; wait for that exact mapping and fail with a retryable bind error if it
does not appear. Never use `context.pages[0]` or `context.new_page()` as an
implicit fallback, because that splits the ledger target from the executor's
page and can create duplicate user-visible tabs.

The executor request deadline is fail-stop. When it expires, Browserwright
flushes a terminal response, terminates that exact executor instance (including
its Playwright driver), and waits for daemon-confirmed process death before the
CLI returns. Browser tabs survive, but executor `state` is lost, Python
`finally` blocks are not guaranteed, and webpage side effects are not rolled
back. A Playwright action timeout that returns before the outer request
deadline is an ordinary request error and does not recycle the executor.

`reset()` is also terminal for its current code request; statements after it do
not run. Both `reset()` and `browserwright session reset <id>` use the same
tab-preserving executor recycle model. The next browser command cold-starts one
fresh executor and rebinds the ledger target.

## Extension Backend

The extension backend connects to the user's real Chrome through the unpacked
extension relay. It simulates a browser-level CDP connection over a shared
browser, so a session's workspace must be represented by a Chrome tab group.

Hard invariants:

- One extension session owns at most one Chrome tab group.
- Every tab opened, attached, discovered, or created for that session must stay
  inside that session's group.
- Probe tabs and real user-work tabs are not separate workspaces; they belong to
  the same group.
- `runtime.group_id` in the session ledger is durable state, not a cosmetic
  label. It lets the daemon recover the same group after restart.
- In-process relay state is the fast path; ledger `runtime.group_id` is the
  restart/reconnect fallback. A tab-creation path must refresh from these
  sources before asking Chrome to create a new group.

The extension backend does not isolate cookies, localStorage, IndexedDB,
extensions, downloads, or the Chrome profile. All extension sessions share the
user's normal Chrome profile. Tab groups isolate only tab membership and the
daemon's session-scoped visibility.

`--name` is a required human label. For extension sessions it becomes the Chrome
tab group title, but it is not the identity key. Names need not be unique; the
session id and numeric `group_id` are the stable keys.

## RDP Backend

The RDP backend talks to a real browser-level CDP endpoint. Its workspace is the
browser instance, not a tab group.

For `rdp --create`, the daemon lazily launches and owns an isolated Chrome for
the session, using the ledger workspace port/profile information. Ending the
session tears down that daemon-owned browser.

For `rdp --attach`, the daemon connects to the browser exposed by the recorded
target port. Ending the session must not close that externally-owned browser.

Hard invariants:

- Do not create Chrome tab groups for RDP sessions.
- Do not use the extension tab-group model as a substitute for browser-level
  isolation in RDP.
- Do not make RDP session routing depend on the shared extension relay.
- `--name` is only a session label in RDP. It is not a Chrome tab group title.

## Env Backend

The env backend binds a session's agent surface to a browser the daemon did not
launch — an external browser-level CDP endpoint the daemon resolved from
`BD_CDP_WS` (verbatim) or `BD_CDP_URL` (via `/json/version`). Its workspace is
that browser instance, not a tab group.

An env session is **attach-owned**: ending it reaps the session's executor but
never closes the external browser (same teardown as `rdp --attach`). It has no
per-session `workspace` — it routes to the daemon's shared upstream, so the
externally-owned browser is whatever that one daemon was started against.

Hard invariants:

- Do not create Chrome tab groups for env sessions.
- The unified tab-lifecycle verbs (`openBackgroundTab` / `closeTab` /
  `recoverSession` / `attachActiveTab` / userscript) run their **raw
  browser-level CDP** implementation (`Target.createTarget` / `closeTarget`,
  etc.) for env — the same path as rdp, driven through the shared context's
  `_upstream_command`. env is not the extension relay; never route it through
  the relay-callback synthesis. The discriminator is `Router._raw_cdp_backend`
  ("backend is not `extension`"), not a name check against `rdp`.
- Ending an env session must not close the external browser.

### Scaling env to N profiles

A single daemon has exactly one shared upstream, hence one env browser. To drive
N external profiles (e.g. N anti-detect / CloakBrowser profiles) concurrently,
run **N isolated daemons** — one per profile — each with its own:

- `XDG_RUNTIME_DIR` → a distinct daemon socket (the isolation key that replaced
  `BD_NAME`; two daemons cannot share one socket);
- `--facade-port` → a distinct Playwright-facade port;
- `BD_CDP_WS` (or `BD_CDP_URL`) → that profile's CDP endpoint.

Each daemon holds one `env` session bound to its own profile. A minimal fleet
launcher:

```bash
# profiles: "ws://127.0.0.1:8080/api/profiles/<id>/cdp" per anti-detect profile
i=0
for ws in "${PROFILE_WS_URLS[@]}"; do
  rt="$(mktemp -d)"; fp=$((19990 + ++i))
  XDG_RUNTIME_DIR="$rt" BD_CDP_WS="$ws" \
    browserwright-daemon serve --backend env --facade-port "$fp" &
  # bind an agent session on THIS daemon (same XDG_RUNTIME_DIR reaches its socket)
  sid=$(XDG_RUNTIME_DIR="$rt" browserwright session new --backend=env --name="profile-$i")
  # drive it: XDG_RUNTIME_DIR="$rt" browserwright -s "$sid" -e '...'
done
```

The daemons are independent processes; scale up/down by adding/removing them.
This is the substrate for the probe→site-memory→batch pattern: an Opus probe
session `remember()`s a playbook on one profile; Sonnet batch sessions
`load_site_skill()` and replay it on their own profile's daemon.

## Routing And Facade

Layer 2 calls and daemon IPC carry `session_id`. The daemon resolves the
session record, reads its immutable backend, and chooses the correct upstream
context:

- `extension` and `env` sessions use the shared daemon context.
- `rdp` sessions use a per-session `UpstreamContext`, created lazily from the
  ledger record.

The Playwright facade has backend-specific behavior:

- For `rdp` and `env`, the facade is a byte-for-byte browser-level CDP
  passthrough to the session's real Chrome / external CDP endpoint.
- For `extension`, the facade is a synthesis layer over the extension relay. It
  maps browser-level CDP concepts onto the session's tab group and must refresh
  group state before creating targets.

The extension synthesis exists only because the extension relay is not a native
browser-level CDP server. Do not copy that synthesis into RDP paths.

## Teardown

Ending a session follows ownership:

- Extension sessions close the session's agent-owned tabs/group but leave the
  user's real Chrome running.
- RDP create-owned sessions close the daemon-owned Chrome.
- RDP attach sessions leave the external browser running.
- Env sessions (always attach-owned) leave the external browser running.

Executor cleanup is separate from browser ownership and may run for any session.

`endSession` is an **initiate-then-join** verb (issue #32), not a synchronous
request/response verb: its worst case (serial tab closes over a cold extension
reconnect window) outlives any caller's timeout, so the daemon returns at the
**initiate boundary** — after the bounded fast phase (clients revoked,
executor reaped and confirmed dead, phase `terminating`) — and the unbounded
workspace teardown keeps running as a daemon-side task under the same
per-session lock. The caller **joins** by re-issuing `endSession`, which
blocks until the teardown finishes and returns the final result; the CLI
(`session end`) does initiate → progress-printed join → final result, and
`browserwright-daemon ps` exposes the per-session phase so a slow teardown is
distinguishable from a hung daemon.

The atomicity #33 bought is preserved: the `terminating` phase + a pending
marker are published before the lock is released, so a queued/retried
`ensureExecutor` is refused from the initiate moment, never between reap and
tombstone. A failed/partial teardown flips the phase back to `active` and
installs no tombstone, so `endSession` retries resume (extension retry anchors
are written before the first destructive browser write).

## Common Mistakes To Avoid

- Treating tab groups as the universal session workspace. They are extension
  only.
- Creating a second extension tab group when Playwright cold-starts after an
  agent-path probe.
- Assuming extension tab groups isolate login/storage. They do not.
- Using `--name` as a durable identity key. Use `session_id`; use numeric
  `group_id` for extension group recovery.
- Letting a stale facade-cached group id decide where to create a tab. Refresh
  from relay memory and ledger before tab creation.
- Closing an RDP attach browser on `session end`.

## Related Files

- `src/browserwright/session_create.py` creates ledger records and preserves
  backend-specific ownership rules.
- `src/browserwright/session_registry.py` stores immutable session backend
  metadata and runtime state.
- `src/browserwright/session_runtime.py` persists current target and extension
  `group_id` runtime data.
- `src/browserwright/daemon/server/daemon.py` routes sessions to the shared
  context or per-session RDP contexts.
- `src/browserwright/daemon/server/facade.py` routes Playwright facade clients.
- `src/browserwright/daemon/server/facade_extension.py` implements the
  extension-only Playwright synthesis layer.
- `src/browserwright/daemon/server/extension_upstream.py` and
  `src/browserwright/daemon/server/relay.py` own extension tab/group state.
