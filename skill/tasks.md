# Saving a flow as a reusable task

A "task" is a per-site Python file that bundles a reusable browser flow with metadata. The runtime is filesystem-driven — drop the right files in the right place and `browserwright task <site>/<name>` works immediately. No registry, no scaffolding command — **you author the file yourself with the `Write` tool**.

## Storage layout

```
~/.browserwright/site-skills/<eTLD+1>/
  SKILL.md          # one-line site summary, lists tasks
  memory.md         # frontmatter (site, host_patterns, aliases) + free notes
  tasks/
    <name>.py
    <name2>.py
```

Three roots are searched in order: `./site-skills/` (CWD, git-trackable), then `$BS_HOME/site-skills/` (default `~/.browserwright/site-skills/`), then the bundled starter. Write to the second one unless the user wants the task version-controlled with the project.

## Task file template

`~/.browserwright/site-skills/<site>/tasks/<name>.py`:

```python
"""One-line description of what this task does."""

ARGS = {
    "query": {"type": "str", "required": True, "desc": "Search term"},
    "lang":  {"type": "str", "required": False, "default": "en", "desc": "Language code"},
}
OUTPUT = "{title: str, url: str}"
TAGS = ["search", "example"]
REQUIRES_LOGIN = False
ESTIMATED_DURATION_SEC = 5
LAST_VERIFIED = "2026-05-25"

def run(args, ctx=None):
    # `page` / `context` / `snapshot` are injected by the runtime — the same
    # Playwright surface inline execution gets (also on ctx: ctx.page / ctx.context).
    page.goto(f"https://example.com/search?q={args['query']}", wait_until="load")
    return {"title": page.title(), "url": page.url}
```

Module-level constants are all optional except `ARGS` and `run`. The runtime imports the file with `importlib`, injects the Playwright `page` / `context` / `snapshot` (lazily — a task that never touches them opens no browser), validates args against `ARGS`, then calls `run(args, ctx)`. If you define `OUTPUT_SCHEMA`, the runtime validates `run()`'s return shape against it. `ctx.memory` is the parsed frontmatter of the site's `memory.md`; `ctx.page` / `ctx.context` / `ctx.snapshot` mirror the injected globals.

## Site memory.md template

`~/.browserwright/site-skills/<site>/memory.md`:

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

`host_patterns` and `aliases` power query-based discovery (`browserwright list-tasks --query=...`).

## Procedure

1. Confirm with the user that the flow is worth saving. Agree on a name like `<site>/<task>`.
2. Use the `Write` tool to create:
   - `~/.browserwright/site-skills/<site>/tasks/<name>.py` — template above, with the actual REPL code substituted in.
   - `~/.browserwright/site-skills/<site>/memory.md` if the site folder didn't exist.
   - `~/.browserwright/site-skills/<site>/SKILL.md` if missing — one line per task is enough.
3. Run `browserwright task <site>/<name>` once to verify it works end to end.

The filesystem is the database — there is no save/scaffold command. Write the files directly with the `Write` tool, then run `browserwright task <site>/<name>` to confirm.
