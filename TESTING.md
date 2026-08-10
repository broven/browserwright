# Testing Guide

This file is a map of the test suites and when to run them. It intentionally
does not track exact file counts or test counts; those change often, and
`pytest --collect-only` is the source of truth when you need current numbers.

## Policy: every collected test runs

There is no curated allowlist and no deselection hook. `pytest tests/daemon`
and `pytest tests/skill` run **everything they collect**. The only exclusion
is the real-Chrome E2E suite (`tests/daemon/e2e/`), which marks itself
`real_chrome` and self-skips unless explicitly selected by path or
`-m real_chrome`.

If a test is broken, stale, or covers deleted behavior: **delete or fix it** —
never hide it behind a deselection mechanism. Tests that need a live daemon,
real Chrome, or the network belong in `tests/daemon/e2e/` (or behind an
explicit skip guard), never in the default gate.

## Project Layers

```text
AI agent / Claude Code
        ↓
skill/                    Agent-facing skill documentation
        ↓
src/browserwright/        Layer 2: agent API, sessions, memory, primitives
        ↓
src/browserwright/daemon/ Layer 1: browser/CDP connection, proxy, backends
        ↓
Chrome / extension / RDP browser
```

## Suites

| Suite | Runs by default? | Command |
| --- | --- | --- |
| Daemon unit/contract tests (`tests/daemon/`) | yes | `mise run test:daemon` |
| Agent-layer tests (`tests/skill/`) | yes | `mise run test:skill` |
| Mocked skill evals (`evals/`) | yes | `mise run test:evals` |
| pi extension unit tests (`pi-extension/core/`) | yes | `mise run test:pi` |
| Real-Chrome E2E (`tests/daemon/e2e/`) | no — opt-in | `mise run test:e2e` |

## Fast Local Gate

Run this before handing off ordinary code changes:

```bash
mise run test    # = test:daemon + test:skill + test:evals + test:pi; mocked, no Chrome, no network
```

The same gate runs in CI on every push to `main` and every pull request
(`.github/workflows/test.yml`, ubuntu-latest via mise → `mise run install`
→ `mise run test`). Platform-specific behavior (LaunchAgent/launchctl,
Darwin/Windows Chrome discovery) is exercised through monkeypatched
platform tables, so the gate passes on Linux CI without real macOS APIs.

The gate is walled off from the developer's real global daemon by an autouse
fixture in `tests/conftest.py`. It redirects `XDG_RUNTIME_DIR` (so a
`cleanup_endpoint()` cannot unlink the live daemon's control socket, which would
make its watchdog self-exit) and neuters the two daemon cold-start entry points,
with a raising backstop on the low-level detached spawn. `tests/daemon/e2e/` is
exempt — it starts real daemons deliberately, behind its own port isolation.

Before this existed, `mise run test` reliably evicted the machine-global daemon
and left a worktree daemon squatting on port 19989 until someone ran `mise run
teardown`; the damage only surfaced later as a failed `upgrade-global`. If you
add a test that legitimately needs a real daemon, use the e2e harness rather
than removing the wall — `tests/daemon/test_issue57_global_daemon_isolation.py`
asserts it is armed.

> Working from a fresh checkout on a host that already runs a global
> `browserwright` install plus a loaded Chrome extension? Read
> [docs/architecture.md → Independent local dev — never touch global
> state](docs/architecture.md#independent-local-dev--never-touch-global-state)
> first. It covers daemon socket / port isolation, loading a patched
> copy of the extension into a throwaway Chrome profile, and the env
> overrides every command below assumes.

## Daemon Tests

Use this when changing `src/browserwright/daemon/`, backend resolution, proxy
routing, extension relay behavior, userscripts, or daemon CLI behavior:

```bash
uv run pytest tests/daemon -q
```

What lives there today:

```text
tests/daemon/test_coverage_*.py            dense coverage sweeps (proxy, listener, server, CLI, core)
tests/daemon/test_phase_b_*.py             executor core / supervision / registry units
tests/daemon/test_phase_c_foundation_unit.py  Playwright facade discovery + lazy heredoc page
tests/daemon/test_facade_*.py              facade behavior (extension, proxy bypass, unit)
tests/daemon/test_extension_*.py           extension upstream, title marker, version reload
tests/daemon/test_launch_chrome.py         Chrome launch/discovery (fake binaries; no real Chrome)
tests/daemon/test_doctor.py                doctor schema + backend recommendation
tests/daemon/test_stale_daemon*.py         stale/half-alive daemon recovery
tests/daemon/test_ensure_executor_extension_fastfail.py
tests/daemon/test_repro_gh18_extension_ws_closed.py
```

## Agent-Layer Tests

Use this when changing `src/browserwright/` outside the daemon, the skill-facing
CLI, session registry/runtime, primitives, memory, tasks, or install flows:

```bash
uv run pytest tests/skill -q
```

What lives there today:

```text
tests/skill/test_coverage_cli_runtime.py       CLI parsing/dispatch, sessions, tasks, memory commands
tests/skill/test_coverage_mode_b_dense.py      daemon discovery/client, CDP transport
tests/skill/test_coverage_primitives_sweep.py  session-runtime tab helpers, http/site-memory, cdp transport
tests/skill/test_coverage_repl_misc.py         inline run, discovery, schemas, errors, CDP loop
tests/skill/test_cdp_slow_rpc_regression.py    slow-RPC timeout regression
tests/skill/test_install_extension_v04.py      install wizard + extension availability
tests/skill/test_release_versioning.py         release stamping contract for all three artifacts (see RELEASING.md)
```

## pi Extension Tests

```bash
mise run test:pi                       # or: cd pi-extension && node --test 'core/*.test.ts'
```

TypeScript, run by `node --test` with native type stripping — no build step, no
test framework, no network. Needs Node >= 23.6 for unflagged stripping; `node`
is pinned in `mise.toml` for exactly this reason.

The executor is injected throughout `pi-extension/core/`, so the fallback engine
is testable without a browser. That is deliberately where the tests are: a rung
never tried, a JS shell accepted as success, or an empty search result list
returned as an answer all produce **no error** — just quietly worse answers.

Two live harnesses sit outside the gate because they hit real sites and open
tabs in the user's Chrome:

```bash
cd pi-extension
node verify.ts                         # real fetch chain
node verify.ts --search "some query"   # real search chain
```

---

## Real Chrome E2E (opt-in)

Use this before or after changing extension/RDP parity, browser lifecycle,
tab-group session behavior, screenshots, userscripts, or any code that depends
on actual Chrome behavior:

```bash
mise run test:e2e        # = tests/daemon/e2e/run.sh -v
```

These tests use an isolated Chrome for Testing profile, a patched extension
relay URL, and a test daemon port so they do not touch the daily Chrome profile
or production daemon. They are auto-marked `real_chrome` and skipped by the
default gate; select them by path (`pytest tests/daemon/e2e/`) or marker
(`pytest -m real_chrome`). The extension patcher unit test
(`tests/daemon/e2e/test_patch_extension.py`) is the one exception — it needs no
Chrome and runs in the default gate.

Prerequisite if Chrome for Testing is not already installed:

```bash
npx @puppeteer/browsers install chrome@stable --path /tmp/chrome-for-testing
```

## Skill Evals

Use this when changing `skill/SKILL.md`, examples, command-choice guidance, or
memory/session instructions:

```bash
mise run test:evals      # = uv run python evals/run.py --mock (zero-cost, deterministic)
```

Run a single real case when you need a live check:

```bash
uv run python evals/run.py --case cu-01
```

## Suggested Check Order

For most changes:

```bash
mise run test
```

For daemon/proxy/extension changes, add:

```bash
mise run test:e2e
```

For release-like confidence, run the fast gate, real Chrome E2E, and the skill
evals.
