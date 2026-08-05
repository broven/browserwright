# Real-extension E2E tests

These tests spin up a *real* Chrome with the locally-built extension loaded,
a *real* daemon, and drive them through the `browserwright` CLI. They are
**opt-in** and **isolated** -- they do not touch your daily Chrome or the
production daemon on port 19989.

## Prerequisites

**Chrome for Testing** is required for extension-backend tests. Google Chrome
stable (148+) blocks `--load-extension`; Chrome for Testing does not.

    npx @puppeteer/browsers install chrome@stable --path /tmp/chrome-for-testing

The fixture discovers the binary automatically from `/tmp/chrome-for-testing`
or `~/.cache/puppeteer`. RDP-backend tests use your regular Chrome.

## Running

Use the runner — it works from **any git worktree** with no setup:

    # Whole suite (~60-90s)
    tests/e2e/run.sh

    # Pass pytest flags through / target one file
    tests/e2e/run.sh -v
    tests/e2e/run.sh tests/e2e/test_l2_user_flows.py -v

Why a runner instead of plain `uv run pytest tests/e2e/`: the harness drives
`browserwright` via `shutil.which` and spawns the daemon via `sys.executable`,
so BOTH packages must resolve to the *current* checkout. The installed scripts
/ project `.venv`s point at the main checkout, and the two packages are
separate uv projects (no workspace), so plain `uv run` would test
worktree-daemon + stale-skill. `run.sh` layers the sibling worktree's
browserwright into the daemon env with `--with ../browserwright` (all relative
paths) and clears any stale test daemon on port 29989 first.

If you're NOT in a worktree (main checkout, browserwright installed on PATH),
plain `uv run pytest tests/e2e/` / `uv run pytest -m real_chrome` also works.

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
| daemon config path | `~/.config/browserwright-daemon` | `tmp_path` per session |

Nothing escapes the test boundary. You can have your daily Chrome + extension
running while these tests run.

## Headless (opt-in)

A headful Chrome repeatedly steals the active window, which makes the suite
unpleasant to run on the machine you're working on. Set `BW_E2E_HEADLESS=1` to
launch Chrome for Testing with `--headless=new`:

    BW_E2E_HEADLESS=1 tests/daemon/e2e/run.sh

Default is **headful**, deliberately: these tests exist to pin what a real
browser does. `test_l2_background_render.py` **must** run headful -- both cases
compare a backgrounded tab against a real foreground window, and headless has no
foreground, reports every tab visible, and never throttles rAF, so they would
pass without exercising `keepTabRendered()` at all. They skip themselves under
`BW_E2E_HEADLESS=1` rather than pass vacuously. Anything else asserting
visibility, focus, or frame timing should do the same via
`conftest.requires_headful`.

## Artifacts on failure

When a test fails, `_artifacts/<test-name>/` contains:

- `env.txt` -- relevant `BD_*` / `BS_*` env vars at run time
- `daemon.log` / `daemon-rdp.log` -- the daemon's own log, copied out of the
  live daemon at the moment that test failed

Session-level `_artifacts/daemon.log` and `_artifacts/daemon-rdp.log` hold the
daemon's stdout/stderr plus its full log, appended at session teardown.

Why the copying: the daemon routes its logger to `$TMPDIR/browserwright-daemon.log`
and only echoes to stderr when stderr is a TTY (`_wire_logging` in
`daemon/server/listener.py`). These fixtures give it a pipe (never a TTY) and a
throwaway `TMPDIR` they delete at teardown -- so for a long time every daemon log
line was written and then thrown away, and `_artifacts/daemon.log` was always 0
bytes. The fixtures now harvest the file before removing the directory. If you
ever see an empty `daemon.log` again, that harvest is what broke.

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
