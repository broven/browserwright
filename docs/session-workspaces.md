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

## Routing And Facade

Layer 2 calls and daemon IPC carry `session_id`. The daemon resolves the
session record, reads its immutable backend, and chooses the correct upstream
context:

- `extension`, `env`, and `cloud` sessions use the shared daemon context.
- `rdp` sessions use a per-session `UpstreamContext`, created lazily from the
  ledger record.

The Playwright facade has backend-specific behavior:

- For `rdp`, the facade is a byte-for-byte browser-level CDP passthrough to the
  session's real Chrome endpoint.
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

Executor cleanup is separate from browser ownership and may run for any session.

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
