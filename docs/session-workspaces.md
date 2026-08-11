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
| `cdp` create | One daemon-owned Chrome instance/profile for that session | The browser instance/profile | Never create or simulate tab groups |
| `cdp` attach | An externally-owned browser at the session's recorded port or URL | That browser instance | Never create or simulate tab groups |

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

Expect to retry the first bind: on a fresh session the first browser call
frequently returns `PageBindTimeout` (retryable) — retrying the same command
succeeds.

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
  label. It lets the daemon recover the same group after restart — as a
  *candidate*, never as proof.
- A session's group is identified by its **title** — `<name>-BW<sid>`
  (ADR-0009). We write it when the group is created and Chrome restores it
  with the group, which makes it the one anchor that is both ours and survives
  a browser restart. In-process relay state caches the numeric id Chrome
  currently uses; a tab-creation path refreshes from that cache first, then
  falls back to the title lookup.
- There is no second anchor. The numeric groupId is Chrome's handle (recycled
  across restarts, dropped when the group empties) and the ledger no longer
  mirrors it. The visible `-BW` token is what makes a title match structurally
  unable to land on a user-created group.
- **Accepted assumption: titles do not change.** A user who renames the group
  takes it out of the session by that act — we stop finding it and treat it as
  gone, without warning or retry.

The extension backend does not isolate cookies, localStorage, IndexedDB,
extensions, downloads, or the Chrome profile. All extension sessions share the
user's normal Chrome profile. Tab groups isolate only tab membership and the
daemon's session-scoped visibility.

`--name` is a required human label. For extension sessions it becomes the Chrome
tab group title, but it is not the identity key. Names need not be unique; the
session id and numeric `group_id` are the stable keys.

## CDP Backend

The `cdp` backend talks to a real browser-level CDP endpoint. Its workspace is
the browser instance, not a tab group. Two ways in, distinguished by `owner`:

- **`cdp --create`** (create-owned) — the daemon lazily launches and owns an
  isolated Chrome for the session on the port recorded in the ledger workspace.
  Ending the session tears down that daemon-owned browser.
- **`cdp --attach=<port|url>`** (attach-owned) — the daemon connects to a
  browser someone else owns. See the next section; ending the session must not
  close it.

Hard invariants:

- Do not create Chrome tab groups for `cdp` sessions.
- Do not use the extension tab-group model as a substitute for browser-level
  isolation here.
- Do not make `cdp` session routing depend on the shared extension relay.
- `--name` is only a session label for `cdp`. It is not a Chrome tab group title.

## Attaching An External Browser

`cdp --attach=<port|url>` binds a session to a browser the daemon did not
launch. The endpoint is **per-session**, recorded in that session's ledger
`workspace`:

| what you pass | `workspace` | resolved by |
|---|---|---|
| `--attach=9222` | `{"port": 9222}` | `/json/version` on `127.0.0.1:9222` |
| `--attach=ws://…` / `wss://…` | `{"url": "…"}` | used verbatim — no parsing, no rewriting |
| `--attach=http://…` / `https://…` | `{"url": "…"}` | `/json/version` at that URL |

A `ws(s)://` endpoint is passed to Chrome byte-for-byte on purpose: cloud and
anti-detect browsers embed reusable tokens in the URL, and any normalisation
would invalidate them. That also makes the record credential-bearing — the
ledger is `0600`, and every path that prints an endpoint redacts it.

Such a session is **attach-owned**: ending it reaps the executor and closes the
daemon's own websocket, but never closes the external browser. Its workspace is
that browser instance, not a tab group.

Hard invariants:

- Do not create Chrome tab groups for `cdp` sessions.
- The unified tab-lifecycle verbs (`openBackgroundTab` / `closeTab` /
  `recoverSession` / `attachActiveTab` / userscript) run their **raw
  browser-level CDP** implementation (`Target.createTarget` / `closeTarget`,
  etc.). `cdp` is not the extension relay; never route it through the
  relay-callback synthesis. The discriminator is `Router._raw_cdp_backend`
  ("backend is not `extension`"), never a name check.
- Ending an attach-owned session must not close the external browser. This
  holds through one data dependency, not a teardown branch: `_launch_cdp_chrome`
  is the only writer of `cdp_pid`, it runs only when `cdp_owns_browser` is true,
  and every kill path is gated on `cdp_pid is not None`.

### Driving N external profiles

One daemon, N sessions — each with its own endpoint and its own
`UpstreamContext`:

```bash
# profiles: "ws://127.0.0.1:8080/api/profiles/<id>/cdp" per anti-detect profile
i=0
for ws in "${PROFILE_WS_URLS[@]}"; do
  sid=$(browserwright session new --backend=cdp --attach="$ws" --name="profile-$((++i))")
  browserwright -s "$sid" -e '...'
done
```

The shared backend the daemon was started with is irrelevant here: a `cdp`
session routes to its own context regardless, so the ordinary
extension-backed daemon hosts these alongside the user's real Chrome.

> **This replaced an N-isolated-daemons fleet.** Until #38 the endpoint came
> from the process-global `BD_CDP_WS` / `BD_CDP_URL`, so one daemon could reach
> exactly one external browser, and N profiles meant N daemons each with its own
> `XDG_RUNTIME_DIR`, `--facade-port` and endpoint variable. If you find that
> recipe anywhere, it is stale — those variables are no longer read at all.

This is the substrate for the probe→site-memory→batch pattern: an Opus probe
session `remember()`s a playbook on one profile; Sonnet batch sessions
`load_site_skill()` and replay it on their own profile.

## Routing And Facade

Layer 2 calls and daemon IPC carry `session_id`. The daemon resolves the
session record, reads its immutable backend, and chooses the correct upstream
context:

- `extension` sessions use the shared daemon context.
- `cdp` sessions use a per-session `UpstreamContext`, created lazily from the
  ledger record.

The Playwright facade has backend-specific behavior:

- For `cdp`, the facade is a byte-for-byte browser-level CDP
  passthrough to the session's real Chrome / external CDP endpoint.
- For `extension`, the facade is a synthesis layer over the extension relay. It
  maps browser-level CDP concepts onto the session's tab group and must refresh
  group state before creating targets.

The extension synthesis exists only because the extension relay is not a native
browser-level CDP server. Do not copy that synthesis into `cdp` paths.

## Teardown

Ending a session follows ownership:

- Extension sessions close the session's agent-owned tabs/group but leave the
  user's real Chrome running.
- `cdp` create-owned sessions close the daemon-owned Chrome.
- `cdp` attach sessions leave the external browser running.
- Env sessions (always attach-owned) leave the external browser running.

Executor cleanup is separate from browser ownership and may run for any session.

`endSession` is an **initiate-then-join** verb (issue #32), not a synchronous
request/response verb: its worst case (serial tab closes over a cold extension
reconnect window) outlives any caller's timeout, so the daemon returns at the
**initiate boundary** — after the bounded fast phase (clients revoked,
executor reaped and confirmed dead, phase `terminating`) — and the workspace
teardown keeps running as a daemon-side task under the same per-session lock,
bounded at **60s** (ADR-0009). The callers above it are sized to strictly
exceed that (daemon CLI 70s, Layer 2 80s) so none of them can time out while
the teardown is still running — the original symptom this contract fixed. The caller **joins** by re-issuing `endSession`, which
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

**Daemon-death recovery (issue #40):** the daemon is the executor's owner, so
all of the above assumes a reachable daemon. When the daemon is unreachable,
`session end`/`session reset` fall back to a daemon-independent local reap:
Layer 2 reads the executor's on-disk discovery record (pid + start-time
fingerprint — the same graded TERM→KILL discipline as the daemon's startup
orphan sweep) and reaps the executor itself. `session end` then force-drops
the ledger entry when the executor is provably gone (reaped locally, dead, or
absent) instead of keeping it "for retry" forever — a retry against an
unreachable daemon can never succeed, and the orphan otherwise blocks the
next bind with a CDP attach conflict. The workspace is NOT torn down on this
path (no daemon to close tabs/Chrome), which the CLI's success message says
explicitly. When the daemon IS up, its teardown stays authoritative and the
#32 retry/join semantics are unchanged. A restarted daemon additionally
reaps a live record it has no `Popen` handle for (fingerprint-guarded, same
as the sweep) instead of refusing, so `kill-executor`/`endSession` heal the
session even when the executor survived the startup sweep.

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
- Closing an attached browser on `session end`.

## Related Files

- `src/browserwright/session_create.py` creates ledger records and preserves
  backend-specific ownership rules.
- `src/browserwright/session_registry.py` stores immutable session backend
  metadata and runtime state.
- `src/browserwright/session_runtime.py` persists current target and extension
  `group_id` runtime data.
- `src/browserwright/daemon/server/daemon.py` routes sessions to the shared
  context or per-session `cdp` contexts.
- `src/browserwright/daemon/server/facade.py` routes Playwright facade clients.
- `src/browserwright/daemon/server/facade_extension.py` implements the
  extension-only Playwright synthesis layer.
- `src/browserwright/daemon/server/extension_upstream.py` and
  `src/browserwright/daemon/server/relay.py` own extension tab/group state.
