# Testing Guide

This repository has several test suites at different layers of the browser automation stack. Use this guide to understand what each suite covers, when to run it, and how it relates to the project architecture.

## Project layers

```text
AI agent / Claude Code
        ↓
skill/                         Agent-facing skill documentation
        ↓
browser-skill/                 Layer 2: agent API, sessions, memory, primitives
        ↓
browser-daemon/                Layer 1: browser/CDP connection, proxy, backends
        ↓
Chrome / extension / RDP / cloud browser
```

- `browser-daemon` tests mostly prove the low-level browser connection and CDP proxy work.
- `browser-skill` tests mostly prove the agent-facing API, session model, memory, and primitives work.
- `browser-daemon/tests/e2e` and `browser-skill/tests/agent-e2e` exercise larger end-to-end paths.

## Test locations

```text
browser-daemon/tests/              pytest: daemon unit + integration tests
browser-daemon/tests/e2e/          pytest: real Chrome + extension + daemon E2E
browser-skill/tests/               pytest: skill unit + offline integration tests
browser-skill/tests/agent-e2e/     promptfoo + Claude SDK agent E2E, plus harness tests
evals/                             text-level skills-eval (command-choice gating)
.github/workflows/                 CI entry points
```

## 1. `browser-daemon/tests/`

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
browser-daemon/tests/test_auth.py
browser-daemon/tests/test_cloud_backend.py
browser-daemon/tests/test_env.py
browser-daemon/tests/test_extension_upstream.py
browser-daemon/tests/test_proxy.py
browser-daemon/tests/test_relay.py
browser-daemon/tests/test_serve.py
browser-daemon/tests/test_serve_extension.py
browser-daemon/tests/test_launch_chrome.py
browser-daemon/tests/test_schema_lock.py
```

Run:

```bash
cd browser-daemon
python -m pip install -e ".[test]"
python -m pytest tests -q
```

Note: real Chrome E2E tests under `tests/e2e/` are opt-in and are skipped unless explicitly selected.

## 2. `browser-daemon/tests/e2e/`

Real-browser E2E suite.

These tests start:

- a real Chrome for Testing instance
- the real unpacked Chrome extension, patched to a test relay port
- a real `browser-daemon serve`
- `browser-skill` CLI calls against the daemon

Covers:

- daemon smoke/status
- extension connection
- RDP backend resolution
- extension/RDP `page_info` roundtrips
- basic user flows such as open page, query DOM, screenshot
- parity between extension and RDP backend behavior

Files:

```text
browser-daemon/tests/e2e/test_l0_smoke.py
browser-daemon/tests/e2e/test_l1_roundtrip.py
browser-daemon/tests/e2e/test_l2_user_flows.py
browser-daemon/tests/e2e/test_l3_parity.py
browser-daemon/tests/e2e/test_patch_extension.py
```

Run:

```bash
cd browser-daemon
uv run pytest tests/e2e/ -v
# or
uv run pytest -m real_chrome -v
```

Prerequisite:

```bash
npx @puppeteer/browsers install chrome@stable --path /tmp/chrome-for-testing
```

The suite uses isolated ports/profile directories and does not touch the production daemon or daily Chrome profile.

## 3. `browser-skill/tests/`

Skill-layer pytest suite.

Covers:

- CLI behavior
- daemon client and Mode B socket discovery
- session creation/attachment/context/registry/concurrency
- primitives with fake CDP sessions
- memory and site memory
- install wizard logic
- solidify/propose/scaffold
- selftest runner
- subscriptions
- historical regression cases
- skill markdown guidance checks

Representative files:

```text
browser-skill/tests/test_cli.py
browser-skill/tests/test_daemon_client.py
browser-skill/tests/test_mode_b_client.py
browser-skill/tests/test_memory.py
browser-skill/tests/test_primitives_f4_catchup.py
browser-skill/tests/test_primitives_offline.py
browser-skill/tests/test_session_*.py
browser-skill/tests/test_solidify.py
browser-skill/tests/test_subscriptions.py
browser-skill/tests/test_v02_features.py
```

Run:

```bash
cd browser-skill
python -m pip install -e ".[test]"
python -m pytest tests -q
```

Some tests under `browser-skill/tests/agent-e2e/` need additional agent-e2e dependencies; run those separately if collection fails in a minimal environment.

## 4. `browser-skill/tests/agent-e2e/`

Promptfoo + Claude Agent SDK tests for the v2 sub-agent behavior.

This suite asks a real or lightweight Claude provider to perform agent-facing browser tasks and scores the result with promptfoo assertions.

Important files:

```text
browser-skill/tests/agent-e2e/README.md
browser-skill/tests/agent-e2e/promptfooconfig.yaml
browser-skill/tests/agent-e2e/promptfooconfig-trigger.yaml
browser-skill/tests/agent-e2e/provider.py
browser-skill/tests/agent-e2e/provider_trigger.py
browser-skill/tests/agent-e2e/hooks.py
browser-skill/tests/agent-e2e/scorers/
```

Cases:

| Case | Purpose |
|---|---|
| A | Connect, open `example.com`, summarize page |
| B | Save a user preference to memory |
| C | Solidify a recurring task |
| D | Write site memory |
| E | Check skill auto-triggering without daemon |

Run full A-D suite:

```bash
cd browser-skill/tests/agent-e2e
PROMPTFOO_PYTHON=.venv-agent-e2e/bin/python \
PROMPTFOO_PYTHON_TIMEOUT=600000 \
  npx promptfoo eval -c promptfooconfig.yaml --no-cache
```

Run lightweight Case E only:

```bash
cd browser-skill/tests/agent-e2e
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
promptfoo suite under `browser-skill/tests/agent-e2e/` is local/manual.

## Suggested local check order

For normal development:

```bash
cd browser-daemon && python -m pytest tests -q
cd ../browser-skill && python -m pytest tests -q
```

Before changing backend/proxy/extension behavior:

```bash
cd browser-daemon
uv run pytest tests/e2e/ -v
```

Before changing agent-facing docs, memory, sessions, or primitives:

```bash
cd browser-skill/tests/agent-e2e
PROMPTFOO_PYTHON=.venv-agent-e2e/bin/python \
  npx promptfoo eval -c promptfooconfig-trigger.yaml --no-cache
```

Before release:

```bash
# daemon + skill pytest
cd browser-daemon && python -m pytest tests -q
cd ../browser-skill && python -m pytest tests -q

# real browser e2e
cd ../browser-daemon && uv run pytest tests/e2e/ -v

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
