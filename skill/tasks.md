# The site-skill directory

Site knowledge lives in one directory per site. It holds two things that work
together: **memory** (what you learned about the site) and **tasks** (flows you
solidified into replayable Python).

> **The `.py` task contract lives in `browserwright --print-skill`**, under
> *Reusable Flows: tasks* — the file template, every metadata constant, the
> `run(args, ctx)` injection contract, and when to solidify a flow at all. That
> output is generated from the installed package, so it can never disagree with
> the binary you are running. Read it there, not here.
>
> This file covers the rest of the directory: the layout around the task file,
> the `memory.md` frontmatter that powers discovery, and the procedure for
> creating a site folder from scratch.

## Layout

```
site-skills/<eTLD+1>/
  SKILL.md          # one-line site summary, lists tasks
  memory.md         # frontmatter (site, host_patterns, aliases) + free notes
  tasks/
    <name>.py
    <name2>.py
```

The site directory name is the eTLD+1 stem — `news.ycombinator.com` →
`ycombinator.com`, `shop.example.co.uk` → `example.co.uk` — with a short-alias
override table for a handful of hosts where the algorithmic name is unhelpful
(`mail.google.com` → `gmail`, `www.zhipin.com` → `boss-zhipin`).
`bootstrap_site(host)` picks the right stem for you and creates the folder, so
you rarely need to compute it by hand.

The three roots and their precedence are described in the generated doc. In
short: project-local `./site-skills/` shadows `$BS_HOME/site-skills/` shadows
the bundled starter set.

## memory.md

Not only for saved tasks — during ordinary browsing, `remember(host_or_url,
text, section=...)` lazily creates this file. Keep notes short and sanitized;
`remember()` refuses writes that trip a redaction tripwire (high-entropy
strings, `Bearer` tokens, cookie/session keys, absolute user paths, card
numbers) and tells you which one fired on stderr.

```markdown
---
site: example.com
host_patterns: [example.com, www.example.com]
aliases: [example, ex, 例子]
last_updated: 2026-05-19
---

## Notes
Stable selectors, URL patterns, hidden quirks.

## Known traps
Anti-bot, rate limits, layouts that differ logged-in vs anonymous.

## Task history
- task 'search' created 2026-05-19
```

Frontmatter is load-bearing, the prose body is not:

- `host_patterns` — every hostname that should resolve to this directory.
- `aliases` — natural-language handles. These, plus the task's own `TAGS` and
  docstring, are what `browserwright list-tasks --query="..."` matches against,
  so write the words a user would actually say (including in their language).
- `site` / `last_updated` — identity and staleness.

The parsed frontmatter is handed to a task as `ctx.memory`, so selectors you
record here can be read by the task instead of hardcoded twice.

## Creating a site folder from scratch

1. Agree with the user that the flow is worth saving, and on a name — `<site>/<task>`.
2. Write the files with the `Write` tool. The filesystem is the database; there
   is no save or scaffold command.
   - `tasks/<name>.py` — per the generated doc's template, with your actual
     working REPL code substituted in.
   - `memory.md` if the site folder is new — template above.
   - `SKILL.md` if missing — a title line, a sentence on the site, and one
     bullet per task is enough.
3. Run `browserwright task <site>/<name>` once, end to end, before telling the
   user it exists. A task that was never executed as a task is not verified;
   the injected-globals surface differs from what you had in the REPL scratchpad.
