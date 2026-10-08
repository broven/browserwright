# A remote `--attach` takes its proxy from the caller's environment, once, at session open

Status: accepted and implemented (2026-10-08) by #136. Supersedes the
"never proxy the CDP control channel" half of #20.

## Context

Reaching a remote CDP browser (`--attach=<url>`) takes three connections:
`/json/version` discovery over HTTP, the session's own CDP websocket, and one
websocket per Playwright client through the facade bridge. Before #136 they
disagreed about proxies, and all of them asked the wrong process:

- discovery used httpx with `trust_env=True` for any non-loopback endpoint;
- both websockets were hard-coded `proxy=None` (#20);
- every decision read the **daemon's** environment.

The daemon is long-lived and is started either by a LaunchAgent (no proxy
vars) or from a shell (whatever that shell exported). So one
`session new --attach=…` command worked against one daemon and failed against
another. Under a SOCKS `all_proxy` without `socksio`, httpx raised while
*constructing* the client, before it looked at `NO_PROXY` at all.

Sometimes the proxy is needed: a cloud browser behind a corporate egress, for
example. "Never proxy", which is what #20 chose, is too strong.

## Decision

1. **The CLI that opens the session decides.** `session new --attach=<url>` and
   the throwaway sessions behind `markdown` / `search --attach` resolve the
   proxy for the attach URL from their **own** environment:
   `http_proxy` / `https_proxy` / `all_proxy` / `no_proxy`, in either case. They
   read only those variables, never the OS proxy settings that
   `urllib.request.getproxies()` falls back to on macOS. `NO_PROXY` accepts
   `*`, domains (which match subdomains), IPs, and CIDR ranges. Loopback
   targets, including `*.localhost` and `0.0.0.0`, are always direct.
2. **The result is pinned on the session.** It is stored as
   `workspace["proxy"]` in the ledger and is fixed for the session's lifetime.
   Later calls (`-e`, tasks) do not carry or change it. Every reconnect the
   daemon performs on its own (ADR-0013) reuses it. To change the proxy, end
   the session and open a new one.
3. **The daemon never reads proxy env vars of its own.** Discovery uses httpx
   with `trust_env=False` and the pinned proxy. Both websockets get the pinned
   proxy explicitly, or `None`. Proxy variables in the daemon process have no
   effect on any of these connections.
4. **The opt-out is `NO_PROXY` at the call site.** For example,
   `NO_PROXY=100.64.0.0/10 browserwright session new --attach=…`. There is no
   `--proxy` flag.
5. **SOCKS works out of the box.** `httpx[socks]` and `python-socks` are
   default dependencies, because macOS proxy apps export
   `all_proxy=socks5://…` by default.
6. **A dead proxy is reported as a dead proxy.** A connect failure through a
   proxy names the proxy (redacted) and the `NO_PROXY=<host>` bypass, so it
   cannot be mistaken for a dead remote browser.

## Consequences

- #20's scenario (LAN, Tailscale, CloakBrowser) still works when the user's
  proxy app routes those addresses DIRECT, which Surge and Clash do in rule
  mode. Otherwise the user lists them in `NO_PROXY`; the daemon no longer
  guesses.
- Behaviour no longer depends on which daemon is running.
- A proxy URL can carry `user:pass@`. It is redacted wherever a record is
  printed (`session list --json`, daemon logs, errors).
- Verified end to end by `tests/daemon/e2e/test_issue136_attach_proxy.py`,
  which uses recording HTTP and SOCKS5 proxies and a daemon whose own env
  points at a dead proxy.
