# Real-extension E2E tests

These tests spin up a *real* Chrome with the locally-built extension loaded,
a *real* daemon, and drive them through the `browser-skill` CLI. They are
**opt-in** and **isolated** -- they do not touch your daily Chrome or the
production daemon on port 19989.

## Prerequisites

**Chrome for Testing** is required for extension-backend tests. Google Chrome
stable (148+) blocks `--load-extension`; Chrome for Testing does not.

    npx @puppeteer/browsers install chrome@stable --path /tmp/chrome-for-testing

The fixture discovers the binary automatically from `/tmp/chrome-for-testing`
or `~/.cache/puppeteer`. RDP-backend tests use your regular Chrome.

## Running

    # Run the full E2E suite (~60-90s)
    cd browser-daemon
    uv run pytest tests/e2e/

    # Or by marker
    uv run pytest -m real_chrome

    # One file
    uv run pytest tests/e2e/test_l2_user_flows.py -v

The default `uv run pytest tests/` does NOT run these -- they require a head
of display and a few seconds per case, which we keep out of the inner loop.

## Isolation matrix

| Dimension | Production (your daily) | Test (these E2Es) |
|---|---|---|
| daemon extension port | 19989 | 29989 |
| daemon RDP port | default | 29990 |
| daemon `BD_NAME` | `default` | `bd-e2e` |
| Chrome `user-data-dir` | your daily profile | per-test tmpdir |
| Chrome binary (ext tests) | Google Chrome | Chrome for Testing |
| extension `RELAY_URL` | `:19989` (hardcoded) | `:29989` (patched copy) |
| daemon config path | `~/.config/browser-daemon` | `tmp_path` per session |

Nothing escapes the test boundary. You can have your daily Chrome + extension
running while these tests run.

## Artifacts on failure

When a test fails, `_artifacts/<test-name>/` contains:

- `env.txt` -- relevant `BD_*` / `BS_*` env vars at run time
- (session-level) `daemon.log` -- daemon stderr (everything)

This directory is gitignored.

## Adding a new test

1. Pick the right level (L0=smoke, L1=single round-trip, L2=user flow,
   L3=cross-backend parity).
2. If you need Chrome, depend on `ext_ready` (extension backend) or
   `e2e_chrome_rdp` (RDP backend).
3. To drive the skill, use `helpers.run_skill(script, backend=...)`.
4. Keep assertions at the observable level (page_info, DOM, screenshot),
   not at daemon-internal state -- so v2's sub-agent harness can reuse them.

## When this fails

- "port 29989 already in use" -> `lsof -i :29989`, kill the stale daemon.
- "extension never connected within 10s" -> check `_artifacts/daemon.log`;
  most likely the patched `RELAY_URL` is wrong or Chrome failed to load the
  extension dir.
- "Chrome for Testing not found" -> install via
  `npx @puppeteer/browsers install chrome@stable --path /tmp/chrome-for-testing`
- Orphan Chrome after run -> `pgrep -fa bd-e2e | xargs kill`.
