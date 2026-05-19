# browser

An AI-agent–friendly stack for driving a real Chrome from the terminal over CDP.

```
.
├── browser-daemon/   Layer 1 — CDP WebSocket URL resolver (5 backends)
├── browser-skill/    Layer 2 — REPL / primitives / site skills / memory / solidify
├── skill/            Claude Code skill bundle (symlink this to ~/.claude/skills/browser-skill)
├── ai-e2e-tests/     Claude Agent SDK harness exercising 4 user stories end-to-end
├── browser-connection.md   Why this stack exists (CDP discovery, Chrome 144+ popups)
├── HANDOFF-v0.5.md         Version-by-version delivery history
└── REVIEW.md               Independent code review
```

## ⚠️ Before you start

Chrome 144+ fires an "Allow remote debugging?" popup for **every** new CDP WebSocket on the default profile, and **accumulating popups can freeze Chrome**. See `browser-connection.md` for the field notes.

That's why the legacy `autoconnect` backend (which connected via Chrome's `--remote-debugging-port=9222`) was removed in 2026-05. To drive your daily Chrome, use the `extension` backend — load the unpacked relay extension once and the daemon talks to Chrome through it, zero popups. For scripted work, use an isolated Chrome profile (the default install path).

## Prerequisites

- macOS or Linux
- Python 3.11 (`brew install python@3.11` / `pyenv install 3.11`)
- Chrome / Chromium (any flavor)
- `~/.local/bin` on `$PATH`

## Install

Clone the repo, then from the repo root:

```bash
# 1. browser-daemon (Layer 1)
( cd browser-daemon && python3.11 -m venv .venv && .venv/bin/pip install -e . )

# 2. browser-skill (Layer 2)
( cd browser-skill && python3.11 -m venv .venv && .venv/bin/pip install -e . )

# 3. Symlink the two CLIs onto $PATH (no system-Python pollution)
mkdir -p ~/.local/bin
ln -sf "$PWD/browser-daemon/.venv/bin/browser-daemon" ~/.local/bin/browser-daemon
ln -sf "$PWD/browser-skill/.venv/bin/browser-skill"   ~/.local/bin/browser-skill

# 4. (optional) Install as a Claude Code skill — symlink the whole bundle
mkdir -p ~/.claude/skills
ln -sf "$PWD/skill" ~/.claude/skills/browser-skill

# 5. Verify
browser-daemon version
browser-skill version
browser-daemon doctor
```

The symlink approach means any update to the repo is immediately visible to both your shell and Claude Code — no re-install.

## Smoke test

```bash
# Start an isolated Chrome (own profile dir, won't touch your daily Chrome)
browser-daemon launch-chrome --port 9333 --profile bs-smoke --persistent --json

# Drive it
BD_PORT=9333 BD_BACKEND=rdp browser-skill <<'PY'
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

### Three invocation forms

```bash
# (a) Inline heredoc — one-off scripts, all primitives pre-imported
browser-skill <<'PY'
new_tab("https://news.ycombinator.com")
wait_for_load()
print(page_info())
PY

# (b) Long-lived REPL — long sessions, single shared upstream ws
browser-skill repl start
browser-skill exec 'print(page_info())'
browser-skill exec 'click_at_xy(120, 240)'
browser-skill repl status
browser-skill repl stop

# (c) Solidified task — reusable, pre-saved flow
browser-skill list-tasks
browser-skill list-tasks --query="search the web"
browser-skill task wikipedia.org/lookup --title="Wikipedia"
```

### Choose a backend

| Scenario | Backend | How |
|---|---|---|
| Scripts / iterative work *(default)* | `rdp` + isolated Chrome | `browser-daemon launch-chrome --port 9333 --profile bs-dev` + `BD_PORT=9333 BD_BACKEND=rdp` |
| Your daily Chrome | `extension` | `browser-daemon serve --backend extension` + load `browser-daemon/chrome-extension/` once; zero popups |
| Fingerprint browser (AdsPower / MultiLogin / 比特浏览器) | `rdp` | point `BD_PORT` at the tool's exposed port |
| Remote Chrome (Browser Use / Browserless / Hyperbrowser) | `cloud` | `browser-daemon serve --backend cloud --provider <name>` + auth env vars |

Interactive wizard: `browser-skill install` — walks the decision tree and writes your pick to `~/.browser-skill/global.md`.

### Primitives (pre-imported in every REPL)

- **Navigation:** `goto_url`, `new_tab`, `switch_tab`, `list_tabs`, `current_tab`, `ensure_real_tab`
- **Interaction:** `click_at_xy(x, y)`, `type_text`, `press_key`, `fill_input`, `scroll`, `upload_file`
- **Inspection:** `js(code)`, `cdp(method, params)`, `page_info()`, `capture_screenshot()`
- **Waiting:** `wait`, `wait_for_load`, `wait_for_element`, `wait_for_network_idle`
- **HTTP (no browser, for static pages):** `http_get(url)`
- **Memory:** `remember`, `remember_global`, `remember_preference`, `memory_read`
- **Solidify:** `propose_solidify`, `solidify`

Full catalogue in `browser-skill/README.md`.

### Diagnostics

```bash
browser-daemon doctor                  # which backends are live, why each is/isn't usable
browser-daemon list-backends
browser-skill doctor                   # skill-side health
browser-daemon stats --name default    # observability counters when `serve` is running
```

## Claude Code integration

The `skill/` directory at the repo root is a Claude Code skill bundle. Symlink it to `~/.claude/skills/browser-skill` (step 4 of install above) and Claude Code will discover it. After install, prompts like *"open example.com and screenshot it"* or *"scrape the HN front page"* will trigger the skill automatically.

The skill always recommends the isolated-Chrome path, so it won't spam Allow popups against your daily Chrome.

## ai-e2e-tests

```bash
( cd ai-e2e-tests && python3.11 -m venv .venv && .venv/bin/pip install -r requirements.txt )

( cd ai-e2e-tests && .venv/bin/python harness.py --dry-run )   # validate harness, no Claude needed
( cd ai-e2e-tests && .venv/bin/python harness.py )             # real Claude agent, ~5 min
```

Needs `ANTHROPIC_API_KEY` or an authenticated Claude Code OAuth token. See `ai-e2e-tests/README.md`.

## Uninstall

```bash
rm ~/.local/bin/browser-daemon ~/.local/bin/browser-skill
rm ~/.claude/skills/browser-skill
rm -rf browser-daemon/.venv browser-skill/.venv ai-e2e-tests/.venv
rm -rf ~/.cache/browser-daemon ~/.browser-skill
```

## Further reading

- `browser-connection.md` — *why* this stack exists (CDP discovery paths, Chrome 144+ popup mechanics)
- `browser-daemon/README.md` — backend internals, env vars, `config.toml`
- `browser-skill/README.md` — full primitive surface, v0.4 / v0.5 release notes
- `browser-skill/ONBOARDING.md` — contributor-oriented architecture tour
- `browser-skill/design.md` — full specification

## License

TBD — currently un-licensed source-available. Add a `LICENSE` file before publishing.
