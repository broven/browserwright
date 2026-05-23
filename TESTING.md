# Testing Guide

This repository has several test suites at different layers of the browser automation stack. Use this guide to understand what each suite covers, when to run it, and how it relates to the project architecture.

## Project layers

```text
AI agent / Claude Code
        ↓
skill/                         Agent-facing skill documentation
        ↓
browserwright/                 Layer 2: agent API, sessions, memory, primitives
        ↓
browserwright-daemon/                Layer 1: browser/CDP connection, proxy, backends
        ↓
Chrome / extension / RDP / cloud browser
```

- `browserwright-daemon` tests mostly prove the low-level browser connection and CDP proxy work.
- `browserwright` tests mostly prove the agent-facing API, session model, memory, and primitives work.
- `browserwright-daemon/tests/e2e` and `browserwright/tests/agent-e2e` exercise larger end-to-end paths.

## Test locations

```text
browserwright-daemon/tests/              pytest: daemon unit + integration tests
browserwright-daemon/tests/e2e/          pytest: real Chrome + extension + daemon E2E
browserwright/tests/               pytest: skill unit + offline integration tests
browserwright/tests/agent-e2e/     promptfoo + Claude SDK agent E2E, plus harness tests
evals/                             text-level skills-eval (command-choice gating)
.github/workflows/                 CI entry points
```

## 1. `browserwright-daemon/tests/`

Daemon-layer pytest suite.

Covers:

- CLI behavior
- config/env parsing
- auth: bearer/basic/custom headers/mTLS plumbing
- backend resolution: env, RDP, extension, cloud
- CDP proxying and request/response ID translation
- extension relay and mock extension behavior
- unix socket serve mode
- multiclient event/session isolation
- active tab tracking
- Chrome launching
- doctor/schema/observability

Representative files:

```text
browserwright-daemon/tests/test_auth.py
browserwright-daemon/tests/test_cloud_backend.py
browserwright-daemon/tests/test_env.py
browserwright-daemon/tests/test_extension_upstream.py
browserwright-daemon/tests/test_proxy.py
browserwright-daemon/tests/test_relay.py
browserwright-daemon/tests/test_serve.py
browserwright-daemon/tests/test_serve_extension.py
browserwright-daemon/tests/test_launch_chrome.py
browserwright-daemon/tests/test_schema_lock.py
```

Run:

```bash
cd browserwright-daemon
uv sync                 # installs the project + dev group (pytest, pytest-asyncio)
uv run pytest tests -q
```

Note: real Chrome E2E tests under `tests/e2e/` are opt-in and are skipped unless explicitly selected.

## 2. `browserwright-daemon/tests/e2e/`

Real-browser E2E suite.

These tests start:

- a real Chrome for Testing instance
- the real unpacked Chrome extension, patched to a test relay port
- a real `browserwright-daemon serve`
- `browserwright` CLI calls against the daemon

Covers:

- daemon smoke/status
- extension connection
- RDP backend resolution
- extension/RDP `page_info` roundtrips
- basic user flows such as open page, query DOM, screenshot
- parity between extension and RDP backend behavior

Files:

```text
browserwright-daemon/tests/e2e/test_l0_smoke.py
browserwright-daemon/tests/e2e/test_l1_roundtrip.py
browserwright-daemon/tests/e2e/test_l2_user_flows.py
browserwright-daemon/tests/e2e/test_l3_parity.py
browserwright-daemon/tests/e2e/test_patch_extension.py
```

Run:

```bash
cd browserwright-daemon
uv run pytest tests/e2e/ -v
# or
uv run pytest -m real_chrome -v
```

Prerequisite:

```bash
npx @puppeteer/browsers install chrome@stable --path /tmp/chrome-for-testing
```

The suite uses isolated ports/profile directories and does not touch the production daemon or daily Chrome profile.

## 3. `browserwright/tests/`

Skill-layer pytest suite.

Covers:

- CLI behavior
- daemon client and Mode B socket discovery
- session creation/attachment/context/registry/concurrency
- primitives with fake CDP sessions
- memory and site memory
- install wizard logic
- subscriptions
- historical regression cases
- skill markdown guidance checks

Representative files:

```text
browserwright/tests/test_cli.py
browserwright/tests/test_mode_b_client.py
browserwright/tests/test_memory.py
browserwright/tests/test_primitives_f4_catchup.py
browserwright/tests/test_primitives_offline.py
browserwright/tests/test_session_*.py
browserwright/tests/test_subscriptions.py
browserwright/tests/test_v02_features.py
```

Run:

```bash
cd browserwright
uv sync                 # installs the project + dev group (pytest, pytest-asyncio)
uv run pytest tests -q
```

Some tests under `browserwright/tests/agent-e2e/` need additional agent-e2e dependencies; run those separately if collection fails in a minimal environment.

## 4. `browserwright/tests/agent-e2e/`

Promptfoo + Claude Agent SDK tests for the v2 sub-agent behavior.

This suite asks a real or lightweight Claude provider to perform agent-facing browser tasks and scores the result with promptfoo assertions.

Important files:

```text
browserwright/tests/agent-e2e/README.md
browserwright/tests/agent-e2e/promptfooconfig.yaml
browserwright/tests/agent-e2e/promptfooconfig-trigger.yaml
browserwright/tests/agent-e2e/provider.py
browserwright/tests/agent-e2e/provider_trigger.py
browserwright/tests/agent-e2e/hooks.py
browserwright/tests/agent-e2e/scorers/
```

Cases:

| Case | Purpose |
|---|---|
| A | Connect, open `example.com`, summarize page |
| B | Save a user preference to memory |
| C | Author + run a reusable task |
| D | Write site memory |
| E | Check skill auto-triggering without daemon |

Run full A-D suite:

```bash
cd browserwright/tests/agent-e2e
PROMPTFOO_PYTHON=.venv-agent-e2e/bin/python \
PROMPTFOO_PYTHON_TIMEOUT=600000 \
  npx promptfoo eval -c promptfooconfig.yaml --no-cache
```

Run lightweight Case E only:

```bash
cd browserwright/tests/agent-e2e
PROMPTFOO_PYTHON=.venv-agent-e2e/bin/python \
  npx promptfoo eval -c promptfooconfig-trigger.yaml --no-cache
```

View promptfoo results:

```bash
npx promptfoo view
```

This suite is currently intended for local/manual runs rather than required CI.

## 5. `evals/`

Text-level skills-eval harness (see `evals/README.md`). Feeds `skill/SKILL.md` +
a task prompt to an agent CLI and scores the **commands it emits** (not their live
effect) with a two-tier gate: deterministic pattern match (`expected` must hit,
`forbidden` must not, multi-variant to resist overfitting) + an optional LLM judge.
Cheap and deterministic — the fast red/green signal for SKILL.md steering edits.

Run cost-free (canned transcripts), exits 1 on any failure:

```bash
python3 evals/run.py --mock            # zero-cost CI gate
python3 evals/run.py --mock --json     # machine-readable
python3 evals/run.py --case cu-01      # one real run via codex
```

## CI

No CI is set up — all suites run locally. `python3 evals/run.py --mock`
(cost-free, exits 1 on failure) and the pytest suites are the local gates; the
promptfoo suite under `browserwright/tests/agent-e2e/` is local/manual.

## Suggested local check order

For normal development:

```bash
cd browserwright-daemon && python -m pytest tests -q
cd ../browserwright && python -m pytest tests -q
```

Before changing backend/proxy/extension behavior:

```bash
cd browserwright-daemon
uv run pytest tests/e2e/ -v
```

Before changing agent-facing docs, memory, sessions, or primitives:

```bash
cd browserwright/tests/agent-e2e
PROMPTFOO_PYTHON=.venv-agent-e2e/bin/python \
  npx promptfoo eval -c promptfooconfig-trigger.yaml --no-cache
```

Before release:

```bash
# daemon + skill pytest
cd browserwright-daemon && python -m pytest tests -q
cd ../browserwright && python -m pytest tests -q

# real browser e2e
cd ../browserwright-daemon && uv run pytest tests/e2e/ -v

# skills-eval (cost-free, command-choice gate)
cd .. && python3 evals/run.py --mock

# optional/manual: promptfoo full suite
```

## Coverage gaps to keep in mind

The current test base is strong at daemon integration and offline skill behavior. Known weaker areas:

- real Chrome behavior for all high-level primitives, especially typing/clicking/upload/React-style inputs
- Chrome extension JavaScript unit tests
- real cloud backend E2E
- Windows coverage
- long-running daemon soak tests
- multi-agent stress/concurrency tests
- prompt injection and subscription security tests
- direct tests for bundled starter site-skill tasks
