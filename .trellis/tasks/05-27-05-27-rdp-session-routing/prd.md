# Fix RDP session backend routing

## Problem

`browserwright session new --backend=rdp --create --name=...` creates an
isolated Chrome session, but later `browserwright -s <id> -e ...` can enter the
extension/shared-browser path when the executor control-plane request is made.

## Requirement

The returned Browserwright session id must keep all later executor/control-plane
work routed to the ledger-selected backend. In particular, RDP sessions must not
fall through to the shared extension context.

## Acceptance

- Executor control-plane calls carry the Browserwright session identity as a
  Browserwright request parameter, not as CDP's top-level `sessionId`.
- The daemon rejects mismatched request session parameters against the websocket
  `?session=<id>` binding.
- Explicit unknown websocket/facade `?session=<id>` requests fail closed instead
  of routing to the shared extension context.
- The Playwright facade preserves `?session=` through HTTP discovery and scopes
  extension replay/createTarget to the session's tab group.
- RDP attach sessions connect to the recorded port without daemon-launching or
  daemon-owning Chrome.
- Focused skill and daemon regression tests pass.
