# browser

An AI-agent–friendly stack for driving a real Chrome from the terminal over CDP.

```
.
├── browserwright-daemon/   Layer 1 — CDP WebSocket URL resolver (5 backends)
├── browserwright/    Layer 2 — REPL / primitives / site skills / memory / solidify
├── skill/            Claude Code skill bundle (symlink this to ~/.claude/skills/browserwright)
├── browser-connection.md   Why this stack exists (CDP discovery, Chrome 144+ popups)
└── README.md
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
# 1. browserwright-daemon (Layer 1)
( cd browserwright-daemon && python3.11 -m venv .venv && .venv/bin/pip install -e . )

# 2. browserwright (Layer 2)
( cd browserwright && python3.11 -m venv .venv && .venv/bin/pip install -e . )

# 3. Symlink the two CLIs onto $PATH (no system-Python pollution)
mkdir -p ~/.local/bin
ln -sf "$PWD/browserwright-daemon/.venv/bin/browserwright-daemon" ~/.local/bin/browserwright-daemon
ln -sf "$PWD/browserwright/.venv/bin/browserwright"   ~/.local/bin/browserwright

# 4. (optional) Install as a Claude Code skill — symlink the whole bundle
mkdir -p ~/.claude/skills
ln -sf "$PWD/skill" ~/.claude/skills/browserwright

# 5. Verify
browserwright-daemon version
browserwright version
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

### Three invocation forms

```bash
# (a) Inline heredoc — one-off scripts, all primitives pre-imported
browserwright <<'PY'
new_tab("https://news.ycombinator.com")
wait_for_load()
print(page_info())
PY

# (b) Long-lived REPL — long sessions, single shared upstream ws
browserwright repl start
browserwright exec 'print(page_info())'
browserwright exec 'click_at_xy(120, 240)'
browserwright repl status
browserwright repl stop

# (c) Solidified task — reusable, pre-saved flow
browserwright list-tasks
browserwright list-tasks --query="search the web"
browserwright task wikipedia.org/lookup --title="Wikipedia"
```

### Choose a backend

| Scenario | Backend | How |
|---|---|---|
| Scripts / iterative work *(default)* | `rdp` + isolated Chrome | `browserwright-daemon launch-chrome --port 9333 --profile bs-dev` + `BD_PORT=9333 BD_BACKEND=rdp` |
| Your daily Chrome | `extension` | `browserwright-daemon serve --backend extension` + load `browserwright-daemon/chrome-extension/` once; zero popups |
| Fingerprint browser (AdsPower / MultiLogin / 比特浏览器) | `rdp` | point `BD_PORT` at the tool's exposed port |
| Remote Chrome (Browser Use / Browserless / Hyperbrowser) | `cloud` | `browserwright-daemon serve --backend cloud --provider <name>` + auth env vars |

Interactive wizard: `browserwright install` — walks the decision tree and writes your pick to `~/.browserwright/global.md`.

### Primitives (pre-imported in every REPL)

- **Navigation:** `goto_url`, `new_tab`, `switch_tab`, `list_tabs`, `current_tab`, `ensure_real_tab`
- **Interaction:** `click_at_xy(x, y)`, `type_text`, `press_key`, `fill_input`, `scroll`, `upload_file`
- **Inspection:** `js(code)`, `cdp(method, params)`, `page_info()`, `capture_screenshot()`
- **Waiting:** `wait`, `wait_for_load`, `wait_for_element`, `wait_for_network_idle`
- **HTTP (no browser, for static pages):** `http_get(url)`
- **Memory:** `remember`, `remember_global`, `remember_preference`, `memory_read`
- **Solidify:** `propose_solidify`, `solidify`

Full catalogue in `browserwright/README.md`.

### Diagnostics

```bash
browserwright-daemon doctor                  # which backends are live, why each is/isn't usable
browserwright-daemon list-backends
browserwright doctor                   # skill-side health
browserwright-daemon stats --name default    # observability counters when `serve` is running
```

## Claude Code integration

The `skill/` directory at the repo root is a Claude Code skill bundle. Symlink it to `~/.claude/skills/browserwright` (step 4 of install above) and Claude Code will discover it. After install, prompts like *"open example.com and screenshot it"* or *"scrape the HN front page"* will trigger the skill automatically.

The skill always recommends the isolated-Chrome path, so it won't spam Allow popups against your daily Chrome.

## Uninstall

```bash
rm ~/.local/bin/browserwright-daemon ~/.local/bin/browserwright
rm ~/.claude/skills/browserwright
rm -rf browserwright-daemon/.venv browserwright/.venv
rm -rf ~/.cache/browserwright-daemon ~/.browserwright
```

## Further reading

- `TESTING.md` — map of all test suites, what each covers, and how to run them
- `browser-connection.md` — *why* this stack exists (CDP discovery paths, Chrome 144+ popup mechanics)
- `browserwright-daemon/README.md` — backend internals, env vars, `config.toml`
- `browserwright/README.md` — full primitive surface, v0.4 / v0.5 release notes
- `browserwright/ONBOARDING.md` — contributor-oriented architecture tour
- `browserwright/design.md` — full specification

## License

TBD — currently un-licensed source-available. Add a `LICENSE` file before publishing.
