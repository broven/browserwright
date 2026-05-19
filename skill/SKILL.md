---
name: browser-skill
description: Layer-2 CDP browser automation CLI. Use when the user asks to drive a real Chrome via terminal — open pages, click, fill forms, screenshot, scrape, run scripted browser tasks, or build/solidify reusable site skills. Triggers include "browser-skill", "browser-daemon", "drive my Chrome", "automate the browser via CDP", "open this page and click X", "screenshot this URL", "scrape this site", "solidify this flow into a task". Sits on top of browser-daemon (Layer 1, CDP-URL resolver) and bundles three REPL forms, primitives, per-site memory, and a solidify pipeline.
allowed-tools: Bash(browser-skill:*), Bash(browser-daemon:*)
---

# browser-skill

Two CLIs work together:

- **`browser-daemon`** (Layer 1) — resolves a Chrome CDP WebSocket URL. Backends: `env / rdp / extension / cloud`. Also has `launch-chrome` to spawn an isolated Chrome.
- **`browser-skill`** (Layer 2) — the agent-facing surface. REPL forms, primitives, site skills, memory, solidify.

Both ship from the same repo. If they're not on `$PATH`, see the repo's root `README.md` for the install steps.

## Before you do anything: read memory

Open [memory.md](./memory.md) first. It carries the backend capability table and the user's `scenarios:` list — a per-scenario mapping from "kind of work" to "which backend + how to launch it." Each invocation, match the current task to a `when:` entry and use that scenario's backend. Fall back to `default_backend` if nothing matches.

If the user expresses a preference about a kind of work that isn't yet captured (new account, new fingerprint profile, new isolated-Chrome use case), append a new scenario entry to `memory.md` before proceeding.

> **Note**: The legacy `autoconnect` backend (which used Chrome's `--remote-debugging-port=9222` and triggered an Allow popup on every ws handshake) was removed. To drive the user's daily Chrome, use the `extension` backend — load the unpacked extension once and connect via the daemon's relay; zero popups.

## Three invocation forms

### 1. Inline heredoc — quick one-off scripts

```bash
browser-skill <<'PY'
new_tab("https://news.ycombinator.com")
wait_for_load()
print(page_info())
PY
```

All primitives are pre-imported. The daemon auto-resolves. First call may need `BD_PORT=<port> BD_BACKEND=rdp` env if isolated Chrome is running on a non-default port, or `BD_BACKEND=extension` for the extension relay.

### 2. REPL daemon — long sessions, single shared ws

```bash
browser-skill repl start                # single shared upstream ws for the session
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

Tasks live as plain Python files under `~/.browser-skill/site-skills/<host>/tasks/`. To create one, see [the solidify section below](#when-to-suggest-saving-as-a-task).

## Primitives surface (pre-imported in REPL)

**Navigation:** `goto_url`, `new_tab`, `switch_tab`, `list_tabs`, `current_tab`, `current_page`, `ensure_real_tab`, `iframe_target`

**Interaction:** `click_at_xy(x, y)`, `type_text`, `press_key`, `fill_input`, `scroll`, `dispatch_key`, `upload_file`

**Inspection:** `js(code)`, `cdp(method, params)`, `page_info()`, `capture_screenshot()`

**Waiting:** `wait`, `wait_for_load`, `wait_for_element`, `wait_for_network_idle`, `drain_events`

**HTTP (bypass browser for static pages):** `http_get(url)` — combine with `ThreadPoolExecutor` for bulk fetches

**Memory:** `remember`, `remember_global`, `remember_preference`, `memory_read`, `bootstrap_site` — runtime helpers for writing to `~/.browser-skill/*`. To edit this skill's own `memory.md` / per-site files, use the `Write` / `Edit` tools directly.

**Site:** `list_site_skills`, `load_site_skill`, `run_task`, `run_tasks_concurrent`

## Canonical workflows

### Screenshot a page

```bash
browser-daemon launch-chrome --port 9333 --profile bs-dev --persistent &
BD_PORT=9333 BD_BACKEND=rdp browser-skill <<'PY'
new_tab("https://example.com")
wait_for_load()
path = capture_screenshot()         # returns the absolute PNG path (str)
print(f"screenshot saved: {path}")
PY
```

`capture_screenshot()` writes a PNG and returns its absolute path as a string — pass that path to the Read tool to view it, or feed it back through `print()` so the agent sees where to look. It does NOT return bytes.

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

### Persisting a tab handle across heredocs

Every heredoc runs in a fresh Python process — `current_target_id` is lost when the heredoc exits. To stay on the same tab across multiple `browser-skill <<'PY' ... PY` calls, capture the `targetId` from `attach_active()` / `new_tab()` / `open_background()` and pass it back via `switch_tab()`:

```bash
# Heredoc 1 — open the tab, print its handle so the agent captures it.
browser-skill <<'PY'
r = new_tab("https://example.com")
print("TAB:", r["targetId"])         # agent stores this string
wait_for_load()
PY
```

```bash
# Heredoc 2 — re-bind to the same tab. No popup, no re-attach dance,
# and immune to the user clicking another window between calls
# (which is the failure mode of "always grab the focused tab").
browser-skill <<'PY'
switch_tab("<targetId from heredoc 1>")
type_text("hello")
PY
```

```bash
# Heredoc 3 — same handle still works as long as the tab is open
# and the daemon is alive.
browser-skill <<'PY'
switch_tab("<same targetId>")
print(page_info())
PY
```

The `targetId` is stable for the life of the tab and the daemon — it's encoded from Chrome's `tabId`, not an opaque daemon-side token. If the tab is closed before heredoc N, `switch_tab` raises `CDPError` with a "call `attach_active()` / `new_tab()` to get a fresh handle" hint.

For ad-hoc "just drive my current tab" usage, the boilerplate `attach_active()` at the top of every heredoc is still fine — but for multi-step automation, handle-passing is deterministic.

## When to suggest saving as a task

After a working flow, ask the user "Want me to save this as a reusable task?" if **any** of these hold:

- The user mentioned a recurring need ("每天", "每小时", "monitor", "watch", "notify me when X").
- The flow has 3+ non-trivial steps the user would otherwise re-type.
- The user just ran two heredocs with small variations.
- The output looks like a feed, dashboard, or scheduled scrape.

If they say yes, read [tasks.md](./tasks.md) for the storage layout and template, then use the `Write` tool to drop the files into `~/.browser-skill/site-skills/<host>/`. No CLI scaffolding call needed — the filesystem is the database.

## Diagnostics

```bash
browser-daemon doctor               # which backends are live, why each is/isn't usable
browser-daemon list-backends
browser-skill doctor                # skill-side health (venv, daemon reachability, memory dir)
browser-daemon stats --name default # observability counters when serve is running
```

## Memory files

- **[memory.md](./memory.md)** — ships with this skill. Holds the backend capability table and the user's saved preference. The agent reads this on every invocation and writes to `## User preference` when the user expresses a choice. **No `browser-skill install` step exists or is needed** — `memory.md` is already in place when the skill is installed.
- **[tasks.md](./tasks.md)** — ships with this skill. Read on demand, only when about to solidify a flow into a task.
- `~/.browser-skill/global.md` — daemon-level persistent config (port, default backend). Optional. Set via `remember_preference("daemon.preferred_backend", "rdp")`.
- `~/.browser-skill/site-skills/<eTLD+1>/memory.md` — per-site facts. Append-only.

## When NOT to use this skill

- For simple HTTP fetches with no JS — use `curl` / `WebFetch`.
- For docs lookups on libraries — use `context7` MCP.
- When the user explicitly wants Playwright / Selenium semantics — this is raw CDP, no framework.
