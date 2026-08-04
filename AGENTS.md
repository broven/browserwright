> Single source of truth for all code agents working in this repo.
> `CLAUDE.md` / `.cursorrules` just redirect here.

## Run it (clone → install → test)

This repo is harnessed with [mise](https://mise.jdx.dev). After cloning:

```bash
mise trust && mise install   # pin & install python + uv
mise run install             # uv sync --extra ux (deps + dev group)
mise run test                # fast gate: daemon + skill + skill evals
```

See **[ONBOARD.md](ONBOARD.md)** for the full local dev loop, **[docs/architecture.md](docs/architecture.md)**
for architecture orientation, **[TESTING.md](TESTING.md)** for the test-suite map, and
**[RELEASING.md](RELEASING.md)** for cutting a release, fixing the PyPI publish, and updating the global install.
`mise tasks` lists every verb (install / dev / test[:daemon|:skill|:evals|:e2e] / teardown / lint / build / dev-link / upgrade-global / …).

`mise run teardown` reclaims **this worktree's** leaked e2e daemons, orphaned
Chrome, and test artifacts. It is idempotent and deliberately never touches the
machine-global daemon or a sibling worktree, so it is safe to run at any time —
run it after interrupting an e2e run.

## Project Architecture Reference

Before changing session routing, backend semantics, tab creation, Playwright
facade behavior, or teardown semantics, read `docs/session-workspaces.md`.

## Related projects (prior art)

Two adjacent open-source projects browserwright deliberately borrows from. Read
this before touching the relay, the executor protocol, or any default port —
several values are coexistence decisions, not arbitrary picks, and the code
cites both projects by name.

**[playwriter](https://github.com/remorses/playwriter)** — remorses, MIT, Node/TS.
_"Chrome extension & CLI to let agents control your browser. Runs Playwright
snippets in a stateful sandbox. Available as CLI or MCP."_ Same core architecture
as our extension backend: Chrome extension → `chrome.debugger` → local CDP relay
→ Playwright `connectOverCDP`, which sidesteps `--remote-debugging-port` and its
banner that agents can't dismiss. This is the model our executor/facade layer
mirrors, so the couplings are concrete:

- Its relay sits on **19988**. Our two ports are picked to clear it so all
  three coexist on one machine: extension relay **19989** (`daemon/cli.py`) and
  Playwright facade **19990** (`DEFAULT_FACADE_PORT` in `daemon/config.py`).
  Don't renumber either without re-checking that comment.
- `_executor/protocol.py` mirrors its single response object and `[return value]`
  repr; `cli.py` parses "playwriter-style" `-s <session> -e <code>` flags.
- The MV3 service-worker keepalive in `chrome-extension/background.js` is the
  same trick.
- Shared design bets worth knowing: one `execute` tool instead of a dozen
  bespoke ones (models already know the Playwright API), stateful sessions, and
  accessibility snapshots over screenshots.

**[OpenCLI](https://github.com/jackwener/opencli)** — jackwener, Apache-2.0, Node.
_"Make Any Website into CLI & Use your logged-in browser by AI agent."_ Wider
scope than us: 100+ prebuilt site adapters plus ad-hoc browser driving, shipped
to agents as installable **skills** rather than an MCP server, over a browser-
bridge extension + daemon on **19825**. Two borrows are cited in
`daemon/server/relay.py` as "§A.4":

- anti-CSRF `Origin` validation on the ws upgrade (web-page Origins → HTTP 403);
- 3-retry exponential backoff when `chrome.debugger` reports "already attached".

Its adapter/sitemap model — encode a stable path once, then replay it
deterministically instead of letting the agent freestyle-click — is the closest
external analogue to our `site_skills_starter/`.

> Caveat: the `§A.4` spec document is no longer anywhere in the repo, though
> `daemon/server/relay.py` and `errors.py` still cite it. Treat those code
> comments as the surviving record.

## Extension Backend E2E Testing

When testing extension-backend behavior, prefer the existing real-Chrome E2E
harness instead of asking the user to install or reload the extension in their
daily Chrome. `tests/daemon/e2e/conftest.py` and
`tests/daemon/e2e/_real_browser.py` start an isolated daemon, launch Chrome for
Testing with a patched unpacked `chrome-extension/`, and wait for the extension
to connect. Use the `e2e_daemon`, `e2e_chrome`, `patched_ext_dir`, and
`ext_ready` fixtures for full extension-backend verification. If Chrome for
Testing is missing, install it with:

```bash
npx @puppeteer/browsers install chrome@stable --path /tmp/chrome-for-testing
```
