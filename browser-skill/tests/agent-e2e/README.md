# v2 SDK Sub-Agent E2E Tests

Tests whether the `skill/*.md` documentation + `browser-skill` code let a real Claude Agent SDK sub-agent use the browser fluently. Complements v1 (fixture-style, 291 unit / 11 e2e) which tests code correctness directly.

## How to run

### Prerequisites

- Chrome for Testing: `npx @puppeteer/browsers install chrome@stable --path /tmp/chrome-for-testing`
- Auth: `ANTHROPIC_API_KEY` env var, or a logged-in `claude` CLI session

### Full suite (Cases A-D, needs daemon + Chrome)

```bash
cd browser-skill/tests/agent-e2e
PROMPTFOO_PYTHON=.venv-agent-e2e/bin/python \
PROMPTFOO_PYTHON_TIMEOUT=600000 \
  npx promptfoo eval -c promptfooconfig.yaml --no-cache
```

### View results

```bash
npx promptfoo view
```

## Architecture

```
promptfooconfig.yaml      # Case A-D: full daemon + Chrome
hooks.py                  # beforeAll: daemon+Chrome, beforeEach: workspace reset
provider.py               # claude-agent-sdk wrapper for Cases A-D
agent_runner.py           # ClaudeSDKClient wrapper with mock-user
guards.py                 # PreToolUse hook: restrict tools
workspace.py              # Build/reset isolated workspace
scorers/
  case_a.py               # Connect + open + summarize
  case_b.py               # Save preference to memory.md
  case_c.py               # Solidify task (recurring need)
  case_d.py               # Site memory (explicit write)
_workspace/               # gitignored, rebuilt each run
_artifacts/               # gitignored, dumped on failure
```

## Isolation

| Dimension | Production | v1 | v2 |
|---|---|---|---|
| Extension port | 19989 | 29989 | **39989** |
| RDP port | default | 29990 | **39990** |
| `BD_NAME` | default | bd-e2e | **bd-agent-e2e** |

v1 and v2 can run in parallel without conflicts.

## Cases

| Case | What it tests | Key assertions |
|---|---|---|
| A | Connect + open + summarize | Trace shows browser-skill usage + example.com |
| B | Save preference to memory.md | User preference section updated, table intact |
| C | Solidify task (recurring need) | Intent to save, task file created, valid Python |
| D | Site memory (explicit write) | Site memory file created with content |

## Work mode: north-star spec

Cases are a **north-star spec** for the skill's target behavior. When a case goes red:

1. **Fix the skill** (`skill/*.md` or `browser-skill/src/`), not the test
2. **Anti-overfit gate**: does the fix help the whole task class? Do all phrasings still pass?
3. No hardcoded URLs, wording, or special cases

## Model matrix

The default provider uses `claude-sonnet-4-6`. To test with Opus, add a second provider:

```yaml
# Uncomment to add Opus matrix:
# - id: "file://provider.py"
#   label: "opus"
#   config:
#     model: "claude-opus-4-6"
#     max_turns: 25
```

## CI

Deferred. Needs headed Chrome + xvfb (same as v1). For now, run locally.
