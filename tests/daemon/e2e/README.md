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
paths) and clears any stale test daemon left on this worktree's ports first.

If you're NOT in a worktree (main checkout, browserwright installed on PATH),
plain `uv run pytest tests/e2e/` / `uv run pytest -m real_chrome` also works.

The default `uv run pytest tests/` does NOT run these -- they require a head
of display and a few seconds per case, which we keep out of the inner loop.

## Ports: per-worktree, not fixed (issue #44)

Every port the suite binds is **derived from this worktree's path** by
`_e2e_ports.py` — a stable sha256 of the checkout root mapped into
30000-48999 — so sibling worktrees can run e2e **concurrently** on one
machine. `run.sh` and the pytest fixtures both read that module, so they can
never disagree about which ports this worktree owns.

| Port (old fixed) | Used for |
|---|---|
| 29989 | extension relay (`TEST_EXT_PORT`) |
| 29990 | rdp Chrome `--remote-debugging-port` (`TEST_RDP_PORT`) |
| 29991 | L1 rdp facade daemon (`TEST_FACADE_L1_PORT`) |
| 29992 | L1 extension facade daemon (`TEST_FACADE_L1_EXT_PORT`) |
| 29993 | session extension daemon facade (`TEST_FACADE_EXT_PORT`) |
| 29994 | session rdp daemon facade (`TEST_FACADE_RDP_PORT`) |
| (new) | rdp auto-facade daemon facade (`TEST_FACADE_AUTOFACADE_PORT`) |

Collisions are still possible in principle (two worktrees hashing to the same
block); they fail loudly — `run.sh` REFUSES with the holder's pid and cwd, and
the fixtures' `_port_free` guard names the port — never silently.

## Isolation matrix

| Dimension | Production (your daily) | Test (these E2Es) |
|---|---|---|
| daemon extension port | 19989 | per-worktree derived (see above) |
| daemon RDP port | default | per-worktree derived |
| daemon facade port | 19990 (auto-enable) | per-worktree derived (each daemon) |
| Chrome `user-data-dir` | your daily profile | per-test tmpdir |
| Chrome binary (ext tests) | Google Chrome | Chrome for Testing |
| extension `RELAY_URL` | `:19989` (hardcoded) | derived port (patched copy) |
| daemon config path | `~/.config/browserwright-daemon` | `tmp_path` per session |

Nothing escapes the test boundary. You can have your daily Chrome + extension
running while these tests run — including the machine-global daemon: the
harness isolates every test daemon behind its own XDG_RUNTIME_DIR (distinct
socket) and its own derived ports, and the daemon itself refuses to reclaim
ports held by a daemon from a different runtime dir (issue #44 B), so a test
run never signals the global daemon.

## Headless

A headful Chrome repeatedly steals the active window, which makes the suite
hostile to run on the machine you're working on. The two backends have
**opposite defaults**, for a reason:

| Chrome | Launched by | Default | Flag |
|---|---|---|---|
| extension (Chrome for Testing) | `_launch_cft_with_extension` | **headful** | `BW_E2E_HEADLESS=1` → headless |
| rdp (fixture + daemon-owned) | `launch_chrome()` | **headless** | `BW_E2E_RDP_HEADFUL=1` → headful |

**Extension headful by default** because these tests exist to pin what a real
browser does. `test_l2_background_render.py` **must** run headful -- both cases
compare a backgrounded tab against a real foreground window, and headless has no
foreground, reports every tab visible, and never throttles rAF, so they would
pass without exercising `keepTabRendered()` at all. They skip themselves under
`BW_E2E_HEADLESS=1` rather than pass vacuously. Anything else asserting
visibility, focus, or frame timing should do the same via
`conftest.requires_headful`.

**RDP headless by default** because nothing on the rdp side asserts any of
that. Those tests cover executor lifecycle, state persistence, page binding, tab
reuse and timeout reclamation -- all headless-clean. Their windows were pure
collateral damage: measured on a full run, they were 18 of the 44 Chrome windows
the suite popped, every one of them stealing the active window from whoever was
using the machine. Measured before/after on the same tree, making them headless
changed the failure set by **zero** tests.

How it reaches the daemon-owned Chromes: for a create-owned rdp session the
*daemon* calls `launch_chrome()` itself, in its own process, so no fixture
argument can reach it. `conftest._rdp_chrome_headless_env` (session-scoped,
autouse) instead sets `BD_CHROME_EXTRA_ARGS=--headless=new` on the pytest
process, and every daemon spawned from `os.environ.copy()` inherits it.
`BD_CHROME_EXTRA_ARGS` is a general "append this argv to every Chrome we launch"
hook in `daemon/launch_chrome.py`, default-off in production.

### Why `e2e_chrome_rdp` is not session-scoped

It looks like free money -- it already serialises on the fixed `TEST_RDP_PORT`,
and 15 tests each pay a Chrome launch. It was tried and it fails:
`test_cross_heredoc_tab_reuse_rdp` dies with `PageBindTimeout` on the first run
in the default order. For an rdp session the workspace *is* the browser instance
(see `CONTEXT.md`), so one shared Chrome is one shared workspace and a
neighbour's leftover targets are visible to whoever binds next. Since the
Chromes are headless now, sharing them would save no windows at all -- only
flakiness. See the fixture docstring.

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

- "port N already in use" -> `lsof -i :N`, kill the stale daemon. The port is
  derived from this worktree's path (see above); a holder from a *different*
  worktree means the hash collided — `run.sh` will name the holder.
- "run.sh: REFUSING to start — this worktree's e2e ports are held by
  processes NOT from this worktree" -> exactly what it says: a **sibling
  worktree's e2e run in progress** (or a leftover from an older checkout's
  fixed 29989 block). run.sh used to kill unconditionally, which voided both
  runs. Wait for it to finish, or kill it by hand if you know it is dead
  weight.
- "extension never connected within 10s" -> check `_artifacts/daemon.log`;
  most likely the patched `RELAY_URL` is wrong or Chrome failed to load the
  extension dir.
- "Chrome for Testing not found" -> install via
  `npx @puppeteer/browsers install chrome@stable --path /tmp/chrome-for-testing`
- Orphan Chrome after run -> `pgrep -fa bd-e2e | xargs kill`.
