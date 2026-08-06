# browserwright — architecture & contributor orientation

New to this repo? Read [ONBOARD.md](../ONBOARD.md) first for the
clone → install → test loop. This document is the deeper tour: how the layers
fit together, how to develop without touching the machine's global install, and
the operating principles that keep the codebase safe to change.

## The layers

```text
AI agent / Claude Code
        ↓
skill/                    Agent-facing skill shell (points at `browserwright --print-skill`)
        ↓
src/browserwright/        Layer 2: agent CLI, sessions, primitives, site skills, memory
        ↓
src/browserwright/daemon/ Layer 1: the global daemon — CDP proxy, backends, extension relay,
        ↓                 Playwright facade
Chrome (daily via extension relay / daemon-owned isolated via cdp / external via env)
```

- **One long-lived global daemon** on the fixed socket
  `${XDG_RUNTIME_DIR:-/tmp}/browserwright-daemon.sock`. It serves all sessions
  at once: extension sessions share one relay upstream (the user's real
  Chrome), each cdp session gets its own daemon-owned Chrome, env sessions
  bind to an externally-owned CDP endpoint.
- **A session is the isolation key.** Its backend is chosen at
  `browserwright session new` and immutable afterwards; the daemon routes each
  client by reading the session ledger. On extension the session's workspace is
  a Chrome tab group; on cdp/env it is the browser instance.
- **One session, one resident executor, one Playwright controller.** Browser
  code and tasks for a session run FIFO in that executor and reuse its live
  page/context. A request deadline or reset recycles that exact executor and
  waits for confirmed process death; it never closes the session's browser
  tabs.

Authoritative references — read before changing session routing, backend
semantics, tab creation, facade behavior, or teardown:

- [`session-workspaces.md`](session-workspaces.md) — the load-bearing session
  workspace model (invariants, teardown/ownership rules, facade behavior).
- [`refactor-single-daemon.md`](refactor-single-daemon.md) — the design record
  of the single-global-daemon refactor (why `BD_NAME` is gone, the unified
  downstream verbs).
- [`daemon.md`](daemon.md) — Layer 1 backends, env vars, `config.toml`.

## TL;DR rules

- **Talk to the daemon, not the browser.** The skill is Layer 2; raw CDP
  lives in Layer 1. If you find yourself opening a ws to Chrome from Layer 2
  code, you're either writing a test (mock it) or making a mistake (don't).
- **Test policy is non-negotiable.** Chrome 144+ accumulates "Allow remote
  debugging?" popups until it freezes. Two related rules:
  1. *Chrome*: iterative tests go through `browserwright-daemon launch-chrome
     --port <X> --profile /tmp/...` or the e2e harness. Never short-connect to
     the user's daily Chrome.
  2. *Filesystem*: any code path that writes outside the temp dir
     (e.g. `~/.config/browserwright-daemon/config.toml`) must accept a
     `*_PATH` env override so tests can redirect to `tmp_path`. The wizard's
     `BS_DAEMON_CONFIG_PATH` is the canonical example.
- **Develop without touching global state.** The host machine very likely
  already runs a global `browserwright`/`browserwright-daemon` plus a loaded
  Chrome extension; this checkout must coexist with that, not fight it. The
  short version: `uv sync`, then prefix everything with `uv run`, and run
  real-Chrome work through `tests/daemon/e2e/run.sh`, which already isolates
  ports, sockets, profile, and the extension. Full setup below.

## Independent local dev — never touch global state

This section is the cold-start path for a contributor (or a code agent)
who just cloned the repo on a machine where:

- `browserwright` / `browserwright-daemon` are already installed globally
  (e.g. via `uv tool install browserwright`)
- the user's daily Chrome has the unpacked extension loaded against the
  global daemon on `ws://127.0.0.1:19989/`
- the global daemon socket lives at
  `${XDG_RUNTIME_DIR:-/tmp}/browserwright-daemon.sock`

Goal: do all development and verification from THIS worktree without
touching any of the above.

### Collision points to know up front

| Surface | Global default | Risk if you don't isolate |
|---|---|---|
| CLI binary | `~/.local/bin/browserwright{,-daemon}` | invoking `browserwright …` runs the global one against your code |
| Daemon socket | `${XDG_RUNTIME_DIR:-/tmp}/browserwright-daemon.sock` (single fixed name) | two daemons cannot share it — the second refuses to start |
| Extension relay port | `127.0.0.1:19989` | starting a second relay there steals connections from the global extension |
| CDP port | `9222` (Chrome's default) | a stray `--remote-debugging-port=9222` collides with your daily Chrome and pops Allow dialogs |
| Daemon TOML config | `~/.config/browserwright-daemon/config.toml` (override with `BD_CONFIG`) | inherited backend / port settings leak into your dev daemon |
| Skill home | `~/.browserwright/` (override with `BS_HOME`) | site-skill writes and memory leak across instances |
| Chrome profile | the user's daily profile | `--load-extension` / `--remote-debugging-port` ruin it on Chrome 144+ |

Every column on the right has a documented escape — the rest of this
section is how to use them together.

### Step 1: install deps inside the worktree (do NOT touch `~/.local/bin/`)

```bash
uv sync --extra ux          # or just `uv sync`; uv fetches Python 3.11 itself
```

That gives you `.venv/bin/browserwright` and `.venv/bin/browserwright-daemon`
pinned to THIS checkout. Do **not** run `uv tool install` or
`mise run dev-link` from here — both mutate `~/.local/bin/` and put the
worktree on the global PATH. Stay inside the checkout and prefix every
invocation with `uv run`:

```bash
which browserwright                # ~/.local/bin/browserwright  (global)
uv run which browserwright         # .venv/bin/browserwright     (this checkout)
```

If the two answers ever match, your dev session has been hijacking the
global install — back out and rerun under `uv run`.

### Step 2: pick test ports that don't collide

Reuse the e2e harness's port assignments so you never have to remember
which port is which:

| Knob | Production (global) | Isolated dev | Env / flag |
|---|---|---|---|
| extension relay port | 19989 | 29989 | `BD_EXTENSION_PORT` / `--extension-port` |
| cdp port | 9222 | 29990 | `BD_CDP_PORT` / `--port` |
| Playwright facade port | 19990 | 29993 (extension) / 29994 (cdp) | `--facade-port` / `BD_FACADE_PORT` |
| Playwright facade host | `127.0.0.1` (loopback) | tailnet/LAN IP or `0.0.0.0` to reach off-box | `--facade-host` / `BD_FACADE_HOST` |
| daemon socket dir | `${XDG_RUNTIME_DIR:-/tmp}` | `$(mktemp -d)` | `XDG_RUNTIME_DIR` |
| daemon TOML config | `~/.config/browserwright-daemon/config.toml` | none | `BD_CONFIG=""` |
| skill home | `~/.browserwright/` | tmpdir | `BS_HOME` |

The daemon's socket name is fixed (`browserwright-daemon.sock`), so the
**only** way to isolate the socket is to point `XDG_RUNTIME_DIR` at a
throwaway directory. That replaced the old `BD_NAME` / `--name` flag —
do not try to bring those back, they no longer exist.

### Step 3: run the fast mocked suite (no Chrome, no daemon, no extension)

```bash
uv run pytest tests/daemon tests/skill -q
uv run python evals/run.py --mock
```

Both run entirely in-process with mocks. They bind nothing, launch no
Chrome, and do not touch the global daemon or extension. Safe to run
concurrently with the user's daily browsing.

### Step 4: run the `cdp` backend manually (isolated Chrome, no extension needed)

The `cdp` backend launches its own Chrome with `--remote-debugging-port`
and a fresh `--user-data-dir`. No extension involved, no daily-Chrome
collision.

```bash
# 1. Give this daemon its own socket dir (avoids collision with the global one).
export XDG_RUNTIME_DIR="$(mktemp -d)"

# 2. Launch an isolated Chrome via the bundled launcher.
uv run browserwright-daemon launch-chrome \
  --port 29990 --profile bs-dev --persistent --json
#   → {"ws_url":"ws://127.0.0.1:29990/devtools/browser/...","pid":12345,...}

# 3. Drive it through the LOCAL CLI (note `uv run`).
BD_PORT=29990 BD_BACKEND=cdp uv run browserwright <<'PY'
page.goto("https://example.com", wait_until="load")
print(page.title())
PY

# 4. Tear down — kill the Chrome PID from step 2 and remove the runtime dir.
kill 12345
rm -rf "$XDG_RUNTIME_DIR"
unset XDG_RUNTIME_DIR
```

Common pitfall: `BD_BACKEND=cdp` without an explicit `BD_PORT` /
`BD_CDP_PORT` falls back to `9222`, which is your daily Chrome's
discovery port whenever that Chrome is running. Always pin the port.

### Step 5: run the extension backend against a real Chrome — use the e2e harness

The extension backend has two hard collision points with the global
setup, both already solved by the e2e harness:

1. **`chrome-extension/background.js` hardcodes the relay URL** to
   `ws://127.0.0.1:19989/`. Load that dir unmodified into any Chrome and
   it tries to connect to the global daemon. The harness handles this
   via `tests/daemon/e2e/_patch_extension.py`, which copies
   `chrome-extension/` into a tmpdir and rewrites `RELAY_URL` to point
   at the test relay port (29989).
2. **Chrome 148+ (Google-branded stable) blocks `--load-extension`.** You
   must use **Chrome for Testing** instead; install once with:

   ```bash
   npx @puppeteer/browsers install chrome@stable --path /tmp/chrome-for-testing
   ```

   The fixture auto-discovers it under `/tmp/chrome-for-testing` or
   `~/.cache/puppeteer`.

The canonical way to verify extension-backend changes against a real
Chrome is the harness itself:

```bash
# Whole real-Chrome e2e suite (~60-90s, opt-in)
tests/daemon/e2e/run.sh -v

# Or one file
tests/daemon/e2e/run.sh tests/daemon/e2e/test_l1_playwright_facade_extension.py -v
```

`run.sh` does all of the following before pytest starts:

- Kills any stale test daemon on `:29989` (`lsof -ti :29989 | xargs kill`).
- Sets `XDG_RUNTIME_DIR` to a throwaway dir → distinct daemon socket.
- Sets `BD_EXTENSION_PORT=29989`, `BD_CONFIG=""`, `BS_HOME=<test home>`.
- Scrubs every inherited `BD_*` / `BS_*` / `BU_*` env var
  (`scrubbed_env()` in `tests/daemon/e2e/conftest.py`).
- Patches `chrome-extension/` into a tmp dir with
  `RELAY_URL=ws://127.0.0.1:29989/`.
- Launches Chrome for Testing with `--user-data-dir=<tmpdir>` and
  `--load-extension=<patched>` — disposable profile, never the daily one.

End-to-end isolation matrix (kept in sync with `tests/daemon/e2e/README.md`):

| Dimension | Production (daily) | Test (this harness) |
|---|---|---|
| daemon extension port | 19989 | 29989 |
| daemon CDP port | default (9222) | 29990 |
| daemon socket dir | `${XDG_RUNTIME_DIR:-/tmp}` | throwaway tmpdir |
| Chrome `user-data-dir` | the user's daily profile | per-test tmpdir |
| Chrome binary | Google Chrome | Chrome for Testing |
| extension `RELAY_URL` | `ws://127.0.0.1:19989/` | `ws://127.0.0.1:29989/` (patched copy) |
| `BD_CONFIG` | `~/.config/browserwright-daemon/config.toml` | `""` (no file) |
| `BS_HOME` | `~/.browserwright/` | `tests/daemon/e2e/_bs_home/...` |

Nothing escapes the boundary. You can have your daily Chrome + global
extension running while these tests run.

### Step 6: manual extension dev cycle (no harness, full interactive control)

When you want to click the extension popup yourself or watch the daemon
log live, reuse the harness primitives by hand:

```bash
# 1. Patch the extension into a tmpdir, using a non-19989 port.
#    `_patch_extension` lives under tests/daemon/e2e/, and pytest is what
#    normally puts `tests/` on sys.path — outside pytest, prepend it by hand.
ext_dir=$(uv run python -c "
import sys
from pathlib import Path
sys.path.insert(0, 'tests')
from daemon.e2e._patch_extension import patch_extension_dir
print(patch_extension_dir(Path('chrome-extension'), relay_port=29989))
")
echo "patched extension: $ext_dir"

# 2. Run an isolated daemon on the matching port + socket.
export XDG_RUNTIME_DIR="$(mktemp -d)"
BD_CONFIG="" uv run browserwright-daemon serve \
  --backend extension --extension-port 29989 -v &
daemon_pid=$!

# 3. Launch Chrome for Testing with the patched extension + a throwaway profile.
cft=$(ls /tmp/chrome-for-testing/chrome/*/chrome-*/'Google Chrome for Testing.app'/Contents/MacOS/'Google Chrome for Testing' 2>/dev/null | head -1)
"$cft" \
  --user-data-dir=/tmp/bs-dev-profile \
  --load-extension="$ext_dir" &
chrome_pid=$!

# 4. Drive it through the LOCAL CLI.
sid=$(BD_CONFIG="" uv run browserwright session new --backend=extension)
BD_CONFIG="" BD_SESSION=$sid uv run browserwright <<'PY'
page.goto("https://example.com", wait_until="load")
print(page.title())
PY

# 5. Tear down everything.
BD_CONFIG="" uv run browserwright session end --session=$sid
kill $daemon_pid $chrome_pid
rm -rf "$XDG_RUNTIME_DIR" /tmp/bs-dev-profile "$ext_dir"
unset XDG_RUNTIME_DIR
```

Throughout this cycle, the global daemon on `:19989` and the user's
daily Chrome + global extension keep working untouched.

### Step 7: confirm you really were isolated

```bash
# Local CLI is in front of global?
uv run which browserwright           # → .venv/bin/browserwright

# Global daemon still owns 19989?
lsof -iTCP:19989 -sTCP:LISTEN

# Your test daemon was on 29989 (or already torn down)?
lsof -iTCP:29989 -sTCP:LISTEN

# Did anything new appear under the global skill home?
ls -la ~/.browserwright/

# Daemon config untouched?
stat -f '%m' ~/.config/browserwright-daemon/config.toml 2>/dev/null
```

If `lsof :19989` and `lsof :29989` print **different** PIDs (or
`:29989` prints nothing because it's already torn down) and
`~/.browserwright/` has no new entries from this run, isolation held.

### When isolation breaks — quick diagnosis

| Symptom | Likely cause | Fix |
|---|---|---|
| `socket … already in use` on daemon start | another daemon is using the same `XDG_RUNTIME_DIR` socket | `unset XDG_RUNTIME_DIR` and start with a new `mktemp -d`; or `lsof -U` to find the stale process |
| `port 29989 already in use` from `run.sh` | a previous interrupted e2e run left a daemon | `lsof -ti :29989 \| xargs kill` and rerun |
| extension never connects within 10s | the patched `RELAY_URL` doesn't match the daemon's `--extension-port`, or Chrome failed to load the patched dir | check `tests/daemon/e2e/_artifacts/daemon.log` and confirm both numbers match |
| "Chrome for Testing not found" | CfT not installed | `npx @puppeteer/browsers install chrome@stable --path /tmp/chrome-for-testing` |
| your daily Chrome starts popping Allow dialogs | a stray client opened a ws on 9222 against the daily profile | check that every dev invocation sets `BD_PORT` / `BD_CDP_PORT` to 29990, never 9222 |
| `browserwright` resolves to the global binary | you dropped the `uv run` prefix | reinstate it; `uv run which browserwright` must point at `.venv/` |

For deeper context on the e2e harness, see
[`tests/daemon/e2e/README.md`](../tests/daemon/e2e/README.md) — its
"Isolation matrix" and "When this fails" sections are authoritative for
the real-Chrome path.

## Pick a backend — decision tree

Most new contributors come in via "I'm setting up to test something on
this machine." Here's how to choose:

```
Are you running scripted / iterative tests?
├── yes → use the isolated profile (wizard option 1 — recommended).
│         `browserwright-daemon launch-chrome --port 9333 --profile /tmp/bs-dev`
│         then `BD_PORT=9333 BD_BACKEND=cdp browserwright ...`
└── no → are you driving the user's daily Chrome?
        ├── yes → extension backend — load the unpacked relay
        │         extension once; subsequent calls reuse the same ws,
        │         zero popups.
        └── do you have a special browser source?
            ├── fingerprint browser (AdsPower / MultiLogin / GoLogin /
            │   比特浏览器) → cdp attach, supply the port your tool exposes
            └── an externally-owned browser exposing a browser-level CDP ws
                (anti-detect profile, e.g. CloakBrowser) → env:
                `BD_CDP_WS=ws://… browserwright-daemon serve --backend env`,
                then `browserwright session new --backend=env`. Attach-owned —
                `session end` never closes it. N profiles → N isolated daemons.
```

The install wizard codifies this same decision tree —
`browserwright install` and answer the prompts.

## Repo layout (start here, then expand)

```
src/browserwright/
├── cli.py                ← argv dispatch — start here when wiring a new subcommand
├── __init__.py           ← `EXPORTS` = the `from browserwright import *` surface
├── install.py            ← the wizard (doctor-driven option detection)
├── mode_b_client.py      ← Mode B socket client + client_for_session() resolver
├── session_create.py / session_registry.py / session_runtime.py
│                         ← session ledger: creation, immutable backend, runtime state
├── repl/                 ← inline heredoc execution + long-lived REPL daemon
├── primitives/           ← agent-facing API surface (page / interact / inspect / site)
├── memory/               ← `~/.browserwright/global.md` + per-site memory
├── daemon/               ← Layer 1: the global daemon, backends, relay, facade
│   ├── cli.py            ← `browserwright-daemon` argparse + dispatch, nothing else
│   ├── _rpc.py           ← one-shot BrowserwrightDaemon.* RPC over the control socket
│   ├── probe.py          ← daemon liveness observations, shared by `status` + `serve`
│   ├── supervise.py      ← the one graceful→forced process-termination loop
│   ├── _stale.py         ← detect + reclaim a half-alive daemon's relay/facade ports
│   ├── launchagent.py    ← macOS service registration (install / uninstall / restart)
│   ├── relay_status.py   ← the relay's /__status__ endpoint, fetched from one place
│   └── server/           ← listener, Router/proxy, relay, facade, executor registry
└── site_skills_starter/  ← bundled site dirs (names = eTLD+1 stems)

tests/
├── daemon/               ← Layer 1 tests (+ e2e/ real-Chrome harness)
├── skill/                ← Layer 2 tests (CLI, sessions, primitives, memory, install)
└── conftest.py           ← shared fixtures (tmp_bs_home, fresh_modules)
```

When in doubt, `uv run pytest tests/daemon tests/skill -q` runs the mocked
suite — no real daemon, no real Chrome. See [TESTING.md](../TESTING.md) for
the full test-suite map.

## Operating principles (skim, then refer back)

### 1. Doctor as contract

`browserwright-daemon doctor --json` is contract-bound to **zero ws side
effects**. Every wizard option-availability helper (e.g.
`_extension_backend_available()`) must consume the doctor JSON dict only.
Don't open a CDP ws, don't subprocess a backend-specific `--probe`, don't
probe a remote provider. If you need richer signal than doctor provides,
extend the daemon's doctor schema first.

The install-wizard tests enforce this with a `socket.socket` tripwire — any
new helper that touches the network outside doctor trips it immediately.

### 2. Test filesystem isolation

The wizard writes outside the test tree (`~/.config/browserwright-daemon/...`
for the daemon TOML). Tests must override these paths via env vars
*before* the wizard runs, e.g.:

```python
monkeypatch.setenv("BS_DAEMON_CONFIG_PATH",
                   str(tmp_path / "fake-daemon-config.toml"))
```

Every env override the production code accepts is a deliberate seam for
this purpose. Add a `*_PATH` env override whenever you add a new writer
that targets the user's home — it's not optional.

(This rule is the filesystem analogue of the Chrome popup test policy —
tests must not pollute user state, period.)

### 3. Forward-compat wizard options

A new daemon backend that lists itself in `doctor --json` is
auto-surfaceable by the wizard if you pattern after
`_extension_backend_available()`:

1. Add a new entry to `_OPTIONS` with a `(coming vX.Y)` suffix.
2. Add `_<name>_backend_available()` that returns
   `_backend_available_from(_wizard_doctor_backends(), "<name>")`.
3. In `run()`, branch the menu label rewrite + the disabled-choice exit
   on `<name>_live`.
4. Implement per-option prompt collection + memory schema extension.

No skill release is needed to surface the option once the daemon reports it
as `available=true`.

## Common failure modes (and the fix)

| Symptom | Likely cause | Fix |
|---|---|---|
| `memory show --site=news.ycombinator.com` returns empty but bundled dir exists | pre-v0.3.1 user-written `~/.browserwright/site-skills/news/` shadowing the eTLD+1 stem | The `_read_candidates()` fallback should pick it up automatically; if not, run `browserwright index rebuild` |
| `run_task` rejects a task whose args-schema is flat (`{"q": "str"}`) | flat shape not supported | Use the dict shape: `{"q": {"type": "str", "required": True}}` — `_validate_args_schema` rejects the flat form with a clear `ValueError` |
| Tests pollute `~/.config/browserwright-daemon/` | new code path writes outside tmp_path without an env override | follow the `BS_DAEMON_CONFIG_PATH` pattern; add a `*_PATH` env override to the production code |
| Wizard option still says "coming vX.Y" after the daemon was upgraded | stale doctor probe cache, or daemon binary not on `PATH` | rerun the wizard; `browserwright-daemon doctor --json` must report `available=true` for the option |

## "I'm new — what should I read first?"

In this order:

1. [ONBOARD.md](../ONBOARD.md) — the run-it-locally quickstart.
2. This file.
3. [`session-workspaces.md`](session-workspaces.md) — the session model
   invariants (mandatory before touching routing/teardown/facade code).
4. [TESTING.md](../TESTING.md) — which suite to run for which change.
5. One existing test file matching the area you're touching.
