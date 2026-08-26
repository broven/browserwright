# The daemon exposes one TCP endpoint; unix sockets and executor direct-connect are removed

Status: accepted and implemented (2026-08-26). See CONTEXT.md "endpoint".

## Context

Remote use (a browserwright client on a VPS driving the daemon — and thus the
user's Chrome — on a Mac) was structurally impossible: the CLI/skill control
plane was AF_UNIX-only (`_ipc.py`), and `ensureExecutor` returned a local
executor **socket path** for the client to dial directly ("Fork 2"), which is
meaningless off-box. The only remote path was raw Playwright
`connect_over_cdp` against the facade (ADR-0010), losing the entire
skill/executor surface. The unix socket and the facade were also two parallel
CDP-over-ws entrances doing the same job on different transports.

## Decision

Collapse everything onto **one TCP endpoint** (default port 19990), the only
way any downstream reaches the daemon, local or remote. Three sub-surfaces:

- **cdp surface** — the Playwright/puppeteer-compatible CDP face (formerly
  called the *facade*, a term now retired; the extension-backend synthesis
  layer lives here unchanged).
- **control surface** — the CLI/skill control plane (CDP ws + verbs),
  replacing the unix-socket listener, which is deleted.
- **exec-relay surface** — the executor data plane, relayed through the
  daemon. Clients never connect to an executor socket again; the executor's
  own unix socket becomes a daemon-internal detail, and `ensureExecutor`'s
  contract changes from "here is a socket path" to a readiness confirmation.

Addressing is one URL: `BW_DAEMON_URL` (env) / `--daemon-url` (CLI) / toml
key, default `http://127.0.0.1:19990`. If the URL is **explicitly
configured** — even as localhost — a failed connection is an error; the client
never auto-starts a local daemon. Only the unconfigured default keeps today's
auto-start behavior.

The cutover is a **hard cut**: no dual-transport release. Client and daemon
ship from the same repo; there is no third-party client to keep compatible.

## Security model — deliberate, read before "fixing"

There is **no application-layer authentication**. The endpoint grants
arbitrary code execution (the executor runs arbitrary Python), and the
security boundary is entirely the network layer:

- default bind `127.0.0.1`; remote exposure is an explicit opt-in
  (`--facade-host`-style bind config) expected to sit behind
  Tailscale/WireGuard or an SSH tunnel;
- Origin validation on ws upgrades rejects browser-originated connections
  (the OpenCLI §A.4 discipline already used by the relay).

This was weighed against a `0600` token file (which would have preserved the
unix socket's same-uid semantics) and rejected by the owner: single-user
machine, fully private tailnet, accepted threat model. Consequences accepted
knowingly: any local user or process that can reach the port, and any machine
on the tunnel/tailnet, has full control of the browser and code execution.
Binding a public interface is unsupported and unsafe.

## What this does NOT change

- The extension **relay (19989) stays machine-local** — the extension always
  dials its own machine's daemon. ADR-0010's "only the facade goes remote"
  boundary survives as "only the endpoint goes remote".
- ADR-0010's auto-group semantics for sessionless cdp-surface connections are
  retained; `?session=`-bearing control-surface connections are distinct.
- The daemon still spawns and supervises executors (Fork 1a). Only Fork 2
  (client direct-connect to the executor socket) is reversed, which puts
  execute payloads and large outputs on the daemon's event loop — the relay
  path must stream with backpressure, and a daemon restart now severs live
  executor data planes (previously they survived it).

## Consequences

- Remote use becomes first-class: install browserwright on machine B, set
  `BW_DAEMON_URL` to machine A's endpoint (over a tunnel), and the full
  CLI/skill/executor surface works against machine A's browsers.
- The filesystem-permission auth the unix socket provided is gone (see
  Security model above).
- One transport, one test matrix; the unix/TCP duplication is deleted.
