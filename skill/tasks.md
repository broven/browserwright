# Solidifying a flow into a task

A "task" is a per-site Python file that bundles a reusable browser flow with metadata and a selftest. The runtime is filesystem-driven — drop the right files in the right place and `browser-skill task <site>/<name>` works immediately. No registry, no `solidify()` CLI call needed.

## Storage layout

```
~/.browser-skill/site-skills/<eTLD+1>/
  SKILL.md          # one-line site summary, lists tasks
  memory.md         # frontmatter (site, host_patterns, aliases) + free notes
  tasks/
    <name>.py
    <name2>.py
```

Three roots are searched in order: `./site-skills/` (CWD, git-trackable), then `$BS_HOME/site-skills/` (default `~/.browser-skill/site-skills/`), then the bundled starter. Write to the second one unless the user wants the task version-controlled with the project.

## Task file template

`~/.browser-skill/site-skills/<site>/tasks/<name>.py`:

```python
"""One-line description of what this task does."""
from browser_skill import *

ARGS = {
    "query": {"type": "str", "required": True, "desc": "Search term"},
    "lang":  {"type": "str", "required": False, "default": "en", "desc": "Language code"},
}
OUTPUT = "{title: str, url: str}"
TAGS = ["search", "example"]
REQUIRES_LOGIN = False
ESTIMATED_DURATION_SEC = 5
LAST_VERIFIED = "2026-05-19"

def selftest():
    new_tab("https://example.com")
    return "Example" in page_info()["title"]

def run(args, ctx=None):
    new_tab(f"https://example.com/search?q={args['query']}")
    wait_for_load()
    return {"title": page_info()["title"], "url": current_tab()}
```

Module-level constants are all optional except `ARGS` and `run`. The runtime imports the file with `importlib`, validates args against `ARGS`, runs `selftest()` (cached 24h), then calls `run(args, ctx)`. `ctx.memory` is the parsed frontmatter of the site's `memory.md`.

## Site memory.md template

`~/.browser-skill/site-skills/<site>/memory.md`:

```markdown
---
site: example.com
host_patterns: [example.com, www.example.com]
aliases: [example, ex]
last_updated: 2026-05-19
---

## Notes
Stable selectors, URL patterns, hidden quirks.

## Known traps
Anti-bot, rate limits, layouts that differ logged-in vs anonymous.

## Task history
- task 'search' created 2026-05-19
```

`host_patterns` and `aliases` power query-based discovery (`browser-skill list-tasks --query=...`).

## Procedure

1. Confirm with the user that the flow is worth saving. Agree on a name like `<site>/<task>`.
2. Use the `Write` tool to create:
   - `~/.browser-skill/site-skills/<site>/tasks/<name>.py` — template above, with the actual REPL code substituted in.
   - `~/.browser-skill/site-skills/<site>/memory.md` if the site folder didn't exist.
   - `~/.browser-skill/site-skills/<site>/SKILL.md` if missing — one line per task is enough.
3. Run `browser-skill task <site>/<name>` once to verify it works end to end.

The filesystem is the database. Don't use `browser-skill save` / `propose_solidify()` / `solidify()` — direct file writes are cleaner and don't go through a JSON spec.
