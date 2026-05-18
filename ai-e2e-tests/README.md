# browser-skill AI E2E tests

End-to-end harness that uses the **Claude Agent SDK** to spawn a real Claude
agent which is then asked to drive `browser-skill` via Bash, exercising the
four user stories from `browser-skill/design.md §0`.

This is the most credible "is it usable by an AI agent?" check we have for the
framework. Unit + integration tests prove the wiring is correct; this proves
the seams an agent actually touches are usable.

## What it does

1. Launches an **isolated Chrome** on port 9444 with profile
   `/tmp/ai-e2e-profile` (no popups, doesn't touch user's daily Chrome — per
   the project's `chrome-popup-test-policy` + `chrome-popup-accumulation-bug`
   memory rules).
2. Spawns a Claude agent with:
   - `Bash`, `Read`, `Write` tools (Write only for solidify scaffold writes
     in US3 if browser-skill delegates to the agent — usually it shouldn't).
   - A condensed system prompt teaching `browser-skill` usage.
   - Env: `BD_BACKEND=rdp`, `BD_PORT=9444`, `BS_HOME=/tmp/ai-e2e-bs-home`,
     `PATH` augmented with the browser-skill / browser-daemon venv bins.
3. Issues four user-story prompts in sequence (US1–US4), capturing the full
   transcript per story and asserting framework behaviour from the side
   effects (memory files, task files, page-read content).
4. Tears down Chrome + isolated profile + BS_HOME staging area.

## Prerequisites

- Claude Code CLI is installed (Claude Agent SDK bundles it).
- One of the following auth paths is available:
  - `ANTHROPIC_API_KEY` env var, OR
  - The user is already logged in to Claude Code (OAuth token in
    `~/.claude/.credentials.json` / macOS Keychain — what `claude` uses
    interactively).
- `browser-skill` is installed in `../browser-skill/.venv/`.
- `browser-daemon` is installed in `../browser-daemon/.venv/`.
- macOS or Linux with `python3.11`.

## Setup

```bash
cd ai-e2e-tests
python3.11 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

## Run

```bash
# Full run — needs an authenticated Claude Code / API key.
.venv/bin/python harness.py

# Dry run — exercises the harness's setup / teardown / assertions without
# actually spawning a Claude agent (useful when you don't have credentials
# or want to validate the harness itself):
.venv/bin/python harness.py --dry-run

# Run only one user story:
.venv/bin/python harness.py --only US1
.venv/bin/python harness.py --only US2,US4
```

## Output

After a run:

- `AI-E2E-REPORT.md` — pass/fail per US, transcript excerpts, any framework
  gaps observed.
- `transcripts/<US>.json` — full agent transcript (assistant text + tool calls
  + tool results) per user story.

The harness exits non-zero if any US fails (so it can be used in CI once
auth is configured).

## Why no `pytest`?

The harness pre-dates a deeper test suite. It's a one-file black-box script
because the failure mode we care about — "the agent gets confused / takes a
weird path / framework error message is unactionable" — is best surfaced as a
human-readable transcript dump, not a list of assertion errors. Future work
could wrap each US in a pytest test.

## CI integration

A workflow draft is in `.github/workflows/ai-e2e.yml` at the labs repo root.
Two jobs:

- **`dry-run`** — runs on every PR + push to main, no auth needed, ~30s.
  Strict mode (no `--allow-port-9222-listener`); CI's clean port surface
  means the safety check should pass on its own.
- **`live`** — manual `workflow_dispatch` only, gated behind a
  `run_live=true` input and the presence of a `CLAUDE_CODE_OAUTH_TOKEN`
  secret. Runs all 6 stories (~5 min, ~$0.50/run).

Both jobs run on `macos-14` runners — Chrome is pre-installed there at
the standard `.app` path, sidestepping the `discover_chrome_binary`
homebrew-wrapper bug we hit during development. The workflow uploads
transcripts and the auto-report as artifacts on both success and
failure, so failed live runs leave forensic evidence behind.

Branch protection: require only the `dry-run` job for merge — `live` is
informational.
