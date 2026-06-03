# browserwright — onboarding for new contributors

Stuck inside this repo for the first time? Read this once, top to bottom.
It's the shortest path to "I know what to touch and what to leave alone."

## TL;DR

- **`design.md`** is the spec authority. Disagreements between code and
  design.md → footnote design.md, don't drift the code silently.
- **Talk to the daemon, not the browser.** Skill is Layer 2; raw CDP
  calls live in `browserwright-daemon`. If you find yourself opening a ws to
  Chrome in this repo, you're either writing a test (mock it) or making
  a mistake (don't).
- **Test policy is non-negotiable.** Chrome 144+ accumulates "Allow"
  popups until it freezes. Two related rules:
  1. *Chrome*: iterative tests go through `browserwright-daemon launch-chrome
     --port <X> --profile /tmp/...`. Never short-connect to the user's
     daily Chrome.
  2. *Filesystem*: any code path that writes outside the temp dir
     (e.g. `~/.config/browserwright-daemon/config.toml`) must accept a
     `*_PATH` env override so tests can redirect to `tmp_path`. The
     wizard's `BS_DAEMON_CONFIG_PATH` is the canonical example.
- **Develop without touching global state.** The host machine very likely
  already runs a global `browserwright`/`browserwright-daemon` plus a
  loaded Chrome extension; this checkout must coexist with that, not
  fight it. See [Independent local dev — never touch global
  state](#independent-local-dev--never-touch-global-state) below for the
  full setup. The short version: `uv sync`, then prefix everything with
  `uv run`, and run real-Chrome work through `tests/daemon/e2e/run.sh`,
  which already isolates ports, sockets, profile, and the extension.

## Independent local dev — never touch global state

This section is the cold-start path for a contributor (or a Code Agent)
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
| RDP port | `9222` (Chrome's default) | a stray `--remote-debugging-port=9222` collides with your daily Chrome and pops Allow dialogs |
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
| rdp port | 9222 | 29990 | `BD_RDP_PORT` / `--port` |
| Playwright facade port | 19990 | 29993 (extension) / 29994 (rdp) | `--facade-port` |
| daemon socket dir | `${XDG_RUNTIME_DIR:-/tmp}` | `$(mktemp -d)` | `XDG_RUNTIME_DIR` |
| daemon TOML config | `~/.config/browserwright-daemon/config.toml` | none | `BD_CONFIG=""` |
| skill home | `~/.browserwright/` | tmpdir | `BS_HOME` |

The daemon's socket name is fixed (`browserwright-daemon.sock`), so the
**only** way to isolate the socket is to point `XDG_RUNTIME_DIR` at a
throwaway directory. That replaced the old `BD_NAME` / `--name` flag —
do not try to bring those back, they no longer exist.

### Step 3: run the fast mocked suite (no Chrome, no daemon, no extension)

```bash
uv run pytest tests/daemon tests/skill --ignore=tests/skill/agent-e2e -q
uv run python evals/run.py --mock
```

Both run entirely in-process with mocks. They bind nothing, launch no
Chrome, and do not touch the global daemon or extension. Safe to run
concurrently with the user's daily browsing.

### Step 4: run the `rdp` backend manually (isolated Chrome, no extension needed)

The `rdp` backend launches its own Chrome with `--remote-debugging-port`
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
BD_PORT=29990 BD_BACKEND=rdp uv run browserwright <<'PY'
page.goto("https://example.com", wait_until="load")
print(page.title())
PY

# 4. Tear down — kill the Chrome PID from step 2 and remove the runtime dir.
kill 12345
rm -rf "$XDG_RUNTIME_DIR"
unset XDG_RUNTIME_DIR
```

Common pitfall: `BD_BACKEND=rdp` without an explicit `BD_PORT` /
`BD_RDP_PORT` falls back to `9222`, which is your daily Chrome's
discovery port whenever that Chrome is running. Always pin the port.

### Step 5: run the extension backend against the real Chrome — use the e2e harness

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
| daemon RDP port | default (9222) | 29990 |
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
| your daily Chrome starts popping Allow dialogs | a stray client opened a ws on 9222 against the daily profile | check that every dev invocation sets `BD_PORT` / `BD_RDP_PORT` to 29990, never 9222 |
| `browserwright` resolves to the global binary | you dropped the `uv run` prefix | reinstate it; `uv run which browserwright` must point at `.venv/` |

For deeper context on the e2e harness, see
[`tests/daemon/e2e/README.md`](tests/daemon/e2e/README.md) — its
"Isolation matrix" and "When this fails" sections are authoritative for
the real-Chrome path.

## Pick a backend — decision tree

Most new contributors come in via "I'm setting up to test something on
this machine." Here's how to choose:

```
Are you running scripted / iterative tests?
├── yes → use the isolated profile (wizard option 1 — recommended).
│         `browserwright-daemon launch-chrome --port 9333 --profile /tmp/bs-dev`
│         then `BD_PORT=9333 BD_BACKEND=rdp browserwright ...`
└── no → are you driving the user's daily Chrome?
        ├── yes → option 3 (extension backend) — load the unpacked relay
        │         extension once; subsequent calls reuse the same ws,
        │         zero popups.
        └── do you have a special browser source?
            ├── fingerprint browser (AdsPower / MultiLogin / GoLogin /
            │   比特浏览器) → option 2, supply the port your tool exposes
            └── cloud / remote Chrome (Browser Use, Browserless,
                Hyperbrowser, generic CDP-compatible) → option 4
```

The install wizard codifies this same decision tree —
`browserwright install` and answer the prompts.

## Repo layout (start here, then expand)

```
src/browserwright/
├── cli.py                ← argv dispatch — start here when wiring a new subcommand
├── api.py                ← `from browserwright import *` surface
├── install.py            ← the wizard (~550 LOC; doctor-driven option detection)
├── mode_b_client.py      ← Mode B socket client + client_for_session() resolver
├── repl/
│   ├── inline.py         ← P0 #75 popup-cost abort gate; reads doctor JSON
│   └── server.py         ← long-lived REPL daemon
├── primitives/           ← agent-facing API surface (page / interact / inspect / site)
├── memory/
│   ├── global_mem.py     ← `~/.browserwright/global.md` + dotted-key set_preference
│   └── site_mem.py       ← per-host memory; eTLD+1 stems (with legacy fallback)
├── multitask.py          ← run_tasks_concurrent fan-out
└── site_skills_starter/  ← bundled site dirs (names = eTLD+1 stems)

tests/
├── test_install_extension_v04.py        v0.4 wizard wire (12 tests)
├── test_install_cloud_v05.py            v0.5 cloud wizard + config writer (17 tests)
├── test_e2e_bugs_v031.py                4 AI-E2E bug regressions (22 tests)
├── test_memory.py / test_multitask.py / ...
└── conftest.py                          ← shared fixtures (tmp_bs_home, fresh_modules)
```

When in doubt, `uv run pytest -q` runs the full suite (140 tests as of
v0.5 first wave). Tests are entirely mocked — no real daemon, no real
Chrome.

## Operating principles (skim, then refer back)

### 1. Doctor as contract (spec H3)

`browserwright-daemon doctor --json` is contract-bound to **zero ws side
effects**. Every wizard option-availability helper
(`_extension_backend_available()`, `_cloud_backend_available()`, future
v0.6+) must consume the doctor JSON dict only. Don't open a CDP ws,
don't subprocess a backend-specific `--probe`, don't curl a cloud
provider. If you need richer signal than doctor provides, extend the
daemon's doctor schema first.

`test_install_cloud_v05.py::test_doctor_probe_is_the_only_detection_channel`
enforces this — it patches `socket.socket` to a tripwire. Any new helper
that touches the network outside doctor will trip this test immediately.

### 2. Spec authority + footnote-as-you-go

When a behaviour evolves, footnote `design.md` instead of letting the
code drift silently. Example: spec §A.1 originally said "auto-suggest
`repl start`"; P0 #75 strengthened that to "abort with exit 2". The
codebase tracks the new behaviour, and the spec entry got expanded so
readers don't have to guess which version of the contract holds.

### 3. Test filesystem isolation

The wizard writes outside the test tree (`~/.config/browserwright-daemon/...`
for the daemon TOML). Tests must override these paths via env vars
*before* the wizard runs. Established pattern in
`tests/test_install_cloud_v05.py::_drive_wizard`:

```python
monkeypatch.setenv("BS_DAEMON_CONFIG_PATH",
                   str(tmp_path / "fake-daemon-config.toml"))
```

Every env override the production code accepts is a deliberate seam for
this purpose. Add a `*_PATH` env override whenever you add a new writer
that targets the user's home — it's not optional.

(This rule is the filesystem analogue of `chrome-popup-test-policy` —
tests must not pollute user state, period.)

### 4. Forward-compat wizard options

A new daemon backend that lists itself in `doctor --json` is
auto-surfaceable by the wizard if you pattern after
`_extension_backend_available()` / `_cloud_backend_available()`:

1. Add a new entry to `_OPTIONS` with a `(coming vX.Y)` suffix.
2. Add `_<name>_backend_available()` that returns
   `_backend_available_from(_wizard_doctor_backends(), "<name>")`.
3. In `run()`, branch the menu label rewrite + the disabled-choice exit
   on `<name>_live`.
4. Implement per-option prompt collection + memory schema extension.

No Skill release is needed to surface the option once daemon reports it
as `available=true`. This is the v0.4 / v0.5 design that made the
two-wave delivery model work, and it's the model future backends should
follow.

## Common failure modes (and the fix)

| Symptom | Likely cause | Fix |
|---|---|---|
| `inline heredoc fails with `Target.createTarget requires sessionId in extension backend`` | `new_tab()` doesn't support the extension backend | use `open_background(url)` or `attach_active()` to bind to an existing tab; or run against `BD_BACKEND=rdp` with an isolated profile |
| `memory show --site=news.ycombinator.com` returns empty but bundled dir exists | pre-v0.3.1 user-written `~/.browserwright/site-skills/news/` shadowing the eTLD+1 stem | The `_read_candidates()` fallback should pick it up automatically; if not, run `browserwright index rebuild` |
| `run_task` rejects a task whose args-schema is flat (`{"q": "str"}`) | flat shape not supported | Use the dict shape: `{"q": {"type": "str", "required": True}}` — `_validate_args_schema` rejects the flat form with a clear `ValueError` |
| Tests pollute `~/.config/browserwright-daemon/` | new code path writes outside tmp_path without an env override | follow the `BS_DAEMON_CONFIG_PATH` pattern; add a `*_PATH` env override to the production code |
| Wizard option 4 / 5 still says "coming vX.Y" after the daemon was upgraded | stale doctor probe cache, or daemon binary not on `PATH` | rerun the wizard; `browserwright-daemon doctor --json` must report `available=true` for the option |

## "I'm new — what should I read first?"

In this order:

1. This file (you're here).
2. `design.md` §0 (user stories) + §A.1 (REPL invocation forms).
3. `HANDOFF-v0.5.md` for the version-by-version delivery history.
4. One existing test file matching the area you're touching — e.g.
   `test_install_cloud_v05.py` if you're adding a wizard option.
5. The CHANGELOG-style sections in `README.md` (`v0.4`, `v0.5`) for the
   user-visible shape of recent work.

Then open an issue or grep `TODO(agent)` for tasks looking for hands.