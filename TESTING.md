# Testing Guide

This file is a map of the test suites and when to run them. It intentionally
does not track exact file counts or test counts; those change often, and
`pytest --collect-only` is the source of truth when you need current numbers.

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
Chrome / extension / RDP / cloud browser
```

- `tests/daemon/` covers the daemon, CDP proxy, backends, IPC, extension relay,
  Chrome launching, userscripts, and observability.
- `tests/skill/` covers the agent-facing CLI, sessions, primitives, memory,
  tasks, install flows, and skill guidance.
- `tests/daemon/e2e/` covers real Chrome plus the unpacked extension and daemon.
- `evals/` covers text-level command-choice behavior for the skill docs.

## Fast Local Gate

Run this before handing off ordinary code changes:

```bash
uv run pytest tests/daemon tests/skill -q
python3 evals/run.py --mock
```

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

Representative areas:

```text
tests/daemon/test_proxy.py
tests/daemon/test_relay.py
tests/daemon/test_extension_upstream.py
tests/daemon/test_serve.py
tests/daemon/test_launch_chrome.py
tests/daemon/test_cloud_backend.py
tests/daemon/test_userscripts_parse.py
```

## Agent-Layer Tests

Use this when changing `src/browserwright/` outside the daemon, the skill-facing
CLI, session registry/runtime, primitives, memory, tasks, or install flows:

```bash
uv run pytest tests/skill -q
```

Representative areas:

```text
tests/skill/test_cli.py
tests/skill/test_mode_b_client.py
tests/skill/test_session_*.py
tests/skill/test_primitives_*.py
tests/skill/test_memory.py
tests/skill/test_userscript_verify.py
```

## Real Chrome E2E

Use this before or after changing extension/RDP parity, browser lifecycle,
tab-group session behavior, screenshots, userscripts, or any code that depends
on actual Chrome behavior:

```bash
tests/daemon/e2e/run.sh -v
```

These tests use an isolated Chrome for Testing profile, a patched extension
relay URL, and a test daemon port so they do not touch the daily Chrome profile
or production daemon.

Prerequisite if Chrome for Testing is not already installed:

```bash
npx @puppeteer/browsers install chrome@stable --path /tmp/chrome-for-testing
```

## Skill Evals

Use this when changing `skill/SKILL.md`, examples, command-choice guidance, or
memory/session instructions:

```bash
python3 evals/run.py --mock
```

Run a single real case when you need a live check:

```bash
python3 evals/run.py --case cu-01
```

## Suggested Check Order

For most changes:

```bash
uv run pytest tests/daemon tests/skill -q
python3 evals/run.py --mock
```

For daemon/proxy/extension changes, add:

```bash
tests/daemon/e2e/run.sh -v
```

For release-like confidence, run the fast gate, real Chrome E2E, and the skill
evals.
