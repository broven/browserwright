---
name: browser-skill
description: Layer-2 CDP browser automation CLI. Use when the user asks to drive a real Chrome via terminal — open pages, click, fill forms, screenshot, scrape, run scripted browser tasks, or build/solidify reusable site skills. Triggers include "browser-skill", "browser-daemon", "drive my Chrome", "automate the browser via CDP", "open this page and click X", "screenshot this URL", "scrape this site", "solidify this flow into a task". Sits on top of browser-daemon (Layer 1, CDP-URL resolver) and bundles three REPL forms, primitives, per-site memory, and a solidify pipeline.
allowed-tools: Bash(browser-skill:*), Bash(browser-daemon:*)
---

# browser-skill

Two CLIs work together:

- **`browser-daemon`** (Layer 1) — resolves a Chrome CDP WebSocket URL. Backends: `env / rdp / autoconnect / extension / cloud`. Also has `launch-chrome` to spawn an isolated Chrome.
- **`browser-skill`** (Layer 2) — the agent-facing surface. REPL forms, primitives, site skills, memory, solidify.

Both ship from the same repo. If they're not on `$PATH`, see the repo's root `README.md` for the install steps.

## ⚠️ Safety rule — never hammer the user's daily Chrome

Chrome 144+ fires a fresh "Allow remote debugging?" popup for **every** new CDP WebSocket on the default profile, and **accumulating popups can freeze Chrome**.

**Default path for any scripted / iterative work — isolated Chrome (zero popups, zero banner):**

```bash
browser-daemon launch-chrome --port 9333 --profile bs-dev --persistent &
# then drive it via:
BD_PORT=9333 BD_BACKEND=rdp browser-skill <<'PY'
new_tab("https://example.com")
wait_for_load()
print(page_info())
PY
```

If the user explicitly asks for "their real Chrome" → one popup at `browser-skill repl start`, then reuse the ws for the rest of the session. **Never** loop inline heredocs against the autoconnect backend.

The inline heredoc auto-aborts with exit 2 when the daemon would pick `autoconnect` and no shared ws exists. Escape hatch (one-off CI only): `BS_FORCE_AUTOCONNECT_INLINE=1`.

## Three invocation forms

### 1. Inline heredoc — quick one-off scripts

```bash
browser-skill <<'PY'
new_tab("https://news.ycombinator.com")
wait_for_load()
print(page_info())
PY
```

All primitives are pre-imported. The daemon auto-resolves. First call may need `BD_PORT=<port> BD_BACKEND=rdp` env if isolated Chrome is running on a non-default port.

### 2. REPL daemon — long sessions, autoconnect-friendly

```bash
browser-skill repl start                # one popup here (if autoconnect), then none
browser-skill exec 'print(page_info())'
browser-skill exec 'click_at_xy(120, 240)'
browser-skill repl status
browser-skill repl stop
```

### 3. Task — pre-solidified reusable flow

```bash
browser-skill list-tasks                          # discover bundled site skills
browser-skill list-tasks --query="search the web"
browser-skill task wikipedia.org/lookup --title="Wikipedia"
```

## Primitives surface (pre-imported in REPL)

**Navigation:** `goto_url`, `new_tab`, `switch_tab`, `list_tabs`, `current_tab`, `current_page`, `ensure_real_tab`, `iframe_target`

**Interaction:** `click_at_xy(x, y)`, `type_text`, `press_key`, `fill_input`, `scroll`, `dispatch_key`, `upload_file`

**Inspection:** `js(code)`, `cdp(method, params)`, `page_info()`, `capture_screenshot()`

**Waiting:** `wait`, `wait_for_load`, `wait_for_element`, `wait_for_network_idle`, `drain_events`

**HTTP (bypass browser for static pages):** `http_get(url)` — combine with `ThreadPoolExecutor` for bulk fetches

**Memory:** `remember`, `remember_global`, `remember_preference`, `memory_read`, `bootstrap_site`

**Solidify:** `propose_solidify`, `solidify`

**Site:** `list_site_skills`, `load_site_skill`, `run_task`, `run_tasks_concurrent`

## Canonical workflows

### Screenshot a page

```bash
browser-daemon launch-chrome --port 9333 --profile bs-dev --persistent &
BD_PORT=9333 BD_BACKEND=rdp browser-skill <<'PY'
new_tab("https://example.com")
wait_for_load()
img = capture_screenshot()
print(f"screenshot bytes: {len(img)}")
PY
```

### Click flow (coordinate-first, not selector-first)

```bash
BD_PORT=9333 BD_BACKEND=rdp browser-skill <<'PY'
new_tab("https://news.ycombinator.com")
wait_for_load()
capture_screenshot()             # look at pixel coords
click_at_xy(450, 300)            # click target by xy
wait_for_load()
print(page_info())
PY
```

Drop to `js(...)` only when coordinates are the wrong tool (hidden inputs, 0×0 nodes).

### Bulk-fetch many URLs (no browser)

```bash
BD_PORT=9333 BD_BACKEND=rdp browser-skill <<'PY'
from concurrent.futures import ThreadPoolExecutor
urls = [f"https://example.com/page/{i}" for i in range(1, 50)]
with ThreadPoolExecutor(max_workers=10) as ex:
    htmls = list(ex.map(http_get, urls))
print(f"fetched {len(htmls)}")
PY
```

### Solidify a working flow into a saved task

After running an exploratory REPL session that works:

```bash
browser-skill exec 'propose_solidify()'    # shows readiness + scaffold
browser-skill save <site>/<task-name> --json-spec='{...}'
```

## Diagnostics

```bash
browser-daemon doctor               # which backends are live, why each is/isn't usable
browser-daemon list-backends
browser-skill doctor                # skill-side health (venv, daemon reachability, memory dir)
browser-daemon stats --name default # observability counters when serve is running
```

## Choosing a backend (decision tree)

| Situation | Backend | How |
|---|---|---|
| Scripted / iterative work (default for agent) | `rdp` against isolated Chrome | `browser-daemon launch-chrome --port 9333 --profile bs-dev` + `BD_PORT=9333 BD_BACKEND=rdp` |
| User wants their real Chrome | `autoconnect` + `repl start` | one popup, reuse ws for rest of session |
| User uses fingerprint browser (AdsPower / MultiLogin / GoLogin / 比特浏览器) | `rdp` | point `BD_PORT` at the tool's exposed port |
| User uses unpacked Chrome extension relay (v0.4+) | `extension` | `browser-daemon serve --backend extension` + load the bundled `chrome-extension/` |
| Remote / hosted Chrome (Browser Use, Browserless, Hyperbrowser) | `cloud` | `browser-daemon serve --backend cloud --provider <name>` + auth env vars |

The wizard `browser-skill install` walks the user through the same tree and persists their pick to `~/.browser-skill/global.md`.

## Memory model (three tiers)

- **Global** — `~/.browser-skill/global.md`, frontmatter preferences. Writes need explicit user confirm.
- **Per-site** — `~/.browser-skill/site-skills/<eTLD+1>/memory.md`, append-only by default.
- **In-process** — REPL state, ephemeral.

Read: `browser-skill memory show --global=true` / `--site=github.com`.

Dotted-key set: `remember_preference("daemon.preferred_backend", "rdp")` → nested YAML.

## When NOT to use this skill

- For simple HTTP fetches with no JS — use `curl` / `WebFetch`.
- For docs lookups on libraries — use `context7` MCP.
- When the user explicitly wants Playwright / Selenium semantics — this is raw CDP, no framework.
