# browserwright

Let an AI/code agent drive a real (or isolated) Chrome from the terminal over CDP — open pages, click, type, fill forms, scrape, screenshot — and author **userscripts** the browser runs on matching sites.

One installable package, two CLIs that work together:

- **`browserwright-daemon`** (Layer 1) — resolves a Chrome CDP WebSocket URL and proxies it. Backends: `env / rdp / extension / cloud`. Also `launch-chrome` to spawn an isolated Chrome.
- **`browserwright`** (Layer 2) — the agent-facing surface: sessions, heredoc scripting with pre-imported primitives, reusable site tasks, memory, and userscript management.

```
.
├── src/browserwright/        the package
│   ├── …                     Layer 2 — sessions / primitives / site skills / memory / userscripts
│   └── daemon/               Layer 1 — CDP URL resolver + proxy (env/rdp/extension/cloud backends)
├── chrome-extension/         unpacked relay extension for the `extension` backend
├── skill/                    Claude Code skill bundle (symlink to ~/.claude/skills/browserwright)
├── tests/{skill,daemon}/     test suites
├── docs/                     deeper docs (skill.md, daemon.md, session-model.md, …)
└── browser-connection.md     why this stack exists (CDP discovery, Chrome 144+ popups)
```

## Prerequisites

- macOS or Linux
- Python 3.11 (`brew install python@3.11` / `pyenv install 3.11`)
- Chrome / Chromium (any flavor)
- `~/.local/bin` on `$PATH`
- [`uv`](https://docs.astral.sh/uv/) — manages the venv, lockfile, and Python toolchain (`uv` can fetch Python 3.11 itself)

## Install

From the repo root. If you have [`mise`](https://mise.jdx.dev/), one task does everything (venv + both CLIs on `$PATH` + skill symlink):

```bash
mise run reinstall
```

Manual equivalent:

```bash
# 1. Resolve deps from uv.lock into .venv (editable project + dev group + ux extra).
#    uv creates the venv and pins Python from .python-version (3.11).
uv sync --extra ux

# 2. Put both console scripts on $PATH (no system-Python pollution)
mkdir -p ~/.local/bin
ln -sf "$PWD/.venv/bin/browserwright"        ~/.local/bin/browserwright
ln -sf "$PWD/.venv/bin/browserwright-daemon" ~/.local/bin/browserwright-daemon

# 3. (optional) Install as a Claude Code skill — symlink the whole bundle
mkdir -p ~/.claude/skills
ln -sf "$PWD/skill" ~/.claude/skills/browserwright

# 4. Verify
browserwright-daemon version
browserwright --print-skill | head -1
browserwright-daemon doctor
```

The symlink approach means any update to the repo is immediately visible to both your shell and Claude Code — no re-install.

## Smoke test

```bash
# Start an isolated Chrome (own profile dir, won't touch your daily Chrome)
browserwright-daemon launch-chrome --port 9333 --profile bs-smoke --persistent --json

# Drive it
BD_PORT=9333 BD_BACKEND=rdp browserwright <<'PY'
new_tab("https://example.com")
wait_for_load()
info = page_info()
print(f"URL:   {info.get('url')}")
print(f"Title: {info.get('title')}")
PY
# expected:
#   URL:   https://example.com/
#   Title: Example Domain
```

Clean up: `kill <pid>` (the `launch-chrome --json` output includes the pid).

## Usage

### Sessions: create once, pass everywhere

A **session** is the isolation key that lets multiple agents drive browsers without colliding. Create one, then every later call carries its id (via `--session` or `BD_SESSION`):

```bash
sid=$(browserwright session new --backend=extension --name=research)
BD_SESSION=$sid browserwright <<'PY'
open_background("https://news.ycombinator.com", group="Agent")
print(page_info())
PY
browserwright whoami --session=$sid
browserwright session end --session=$sid
```

A bare heredoc with no session/`BD_PORT` context exits 2 with guidance — the daemon is never silently shared.

### Two invocation forms

```bash
# (a) Inline heredoc — one-off scripts, all primitives pre-imported
BD_SESSION=$sid browserwright <<'PY'
goto_url("https://news.ycombinator.com")
wait_for_load()
print(page_info())
PY

# (b) Solidified task — reusable, pre-saved flow under ~/.browserwright/site-skills/<host>/tasks/
browserwright list-tasks
browserwright list-tasks --query="search the web"
browserwright task wikipedia.org/lookup --title="Wikipedia"
```

### Choose a backend

| Scenario | Backend | How |
|---|---|---|
| Your daily Chrome (logged-in / personal) *(default for "use my browser")* | `extension` | `browserwright session new --backend=extension …` — load `chrome-extension/` once, connect via the daemon's relay; zero popups |
| Scripts / iterative work in throwaway profiles | `rdp` + isolated Chrome | `browserwright-daemon launch-chrome --port 9333 --profile bs-dev` + `BD_PORT=9333 BD_BACKEND=rdp` |
| Fingerprint browser (AdsPower / MultiLogin / 比特浏览器) | `rdp` | point `BD_PORT` at the tool's exposed port |
| Remote Chrome (Browser Use / Browserless / Hyperbrowser) | `cloud` | `browserwright-daemon serve --backend cloud --provider <name>` + auth env vars |

Interactive wizard: `browserwright install` — walks the decision tree and writes your pick.

### Userscripts

Author Tampermonkey-style scripts the `extension` backend injects on matching sites:

```bash
browserwright userscript push ./greet.user.js --verify
browserwright userscript list
browserwright userscript toggle <id>
browserwright userscript logs <id>
browserwright userscript remove <id>
```

### Primitives (pre-imported in every heredoc)

- **Navigation:** `goto_url`, `new_tab`, `open_background`, `switch_tab`, `list_tabs`, `current_tab`, `ensure_real_tab`
- **Interaction:** `click_at_xy(x, y)`, `type_text`, `press_key`, `fill_input`, `scroll`, `upload_file`
- **Inspection:** `js(code)`, `cdp(method, params)`, `page_info()`, `capture_screenshot()`, `snapshot()`, `describe_page()`, `diff_snapshot(before)`
- **Waiting:** `wait`, `wait_for_load`, `wait_for_element`, `wait_for_network_idle`
- **HTTP (no browser, for static pages):** `http_get(url)`
- **Memory:** `remember`, `remember_global`, `remember_preference`, `memory_read`

Full catalogue and guidance in `skill/SKILL.md`.

### Diagnostics

```bash
browserwright-daemon doctor                  # which backends are live, why each is/isn't usable
browserwright-daemon list-backends
browserwright doctor                         # skill-side health
browserwright-daemon stats --name default    # observability counters when `serve` is running
```

## Claude Code integration

The `skill/` directory is a Claude Code skill bundle. Symlink it to `~/.claude/skills/browserwright` (step 3 of install) and Claude Code discovers it. Prompts like *"open example.com and screenshot it"*, *"scrape the HN front page"*, or *"write me a userscript that …"* trigger it automatically.

## Uninstall

```bash
rm ~/.local/bin/browserwright ~/.local/bin/browserwright-daemon
rm ~/.claude/skills/browserwright
rm -rf .venv
rm -rf ~/.cache/browserwright-daemon ~/.browserwright
```

## Further reading

- `TESTING.md` — map of the test suites and how to run them
- `browser-connection.md` — *why* this stack exists (CDP discovery paths, Chrome 144+ popup mechanics)
- `docs/daemon.md` — backend internals, env vars, `config.toml`
- `docs/skill.md` — full primitive surface and release notes
- `docs/session-model.md` — the session isolation model
- `ONBOARDING.md` — contributor-oriented architecture tour

## License

TBD — currently un-licensed source-available. Add a `LICENSE` file before publishing.
