# feedback — mine real transcripts for browserwright friction

`evals/run.py` scores the skill on *synthetic* prompts. This harvester does the
opposite: it mines **real** Claude Code sessions where an agent actually ran the
`browserwright` command, distills each into the friction-relevant story, and
hands it back so the skill docs/CLI can be improved from how the skill is really
used and where it really trips agents up.

## Run it

```bash
python3 evals/feedback/collect.py            # collect new/changed sessions → inbox/<runid>/
python3 evals/feedback/collect.py --list     # preview what would be collected, write nothing
python3 evals/feedback/collect.py --all       # ignore state, re-collect everything qualifying
python3 evals/feedback/collect.py --keep-raw  # also copy full raw .jsonl (default: distilled only)
python3 evals/feedback/collect.py --reset      # forget state (next run re-collects all)
```

## What "qualifying" means

A session qualifies only if it contains a real `browserwright` **invocation** —
a Bash tool call that runs the command (`browserwright <<'PY'`,
`BD_SESSION=$sid browserwright …`, `$(browserwright session new …)`, etc).
Sessions that merely *mention* `browserwright` because `SKILL.md` was loaded as
context do **not** qualify. The detector (`count_invocations`) has a self-test
that runs on every invocation; it rejects paths like `~/.browserwright/…` and
the separate `browserwright-daemon` command.

## Stateful

`.state.json` records every scanned `.jsonl` by byte size. Each run re-emits a
file only if it is **new** or has **grown** (Claude Code appends to a session's
jsonl as the conversation continues), so re-runs are cheap and incremental.

## Output (per run, under `inbox/<runid>/`)

- `distilled/<project>__<id>.md` — the part you feed to Claude. Human prompts +
  every `browserwright` call + its result + the assistant text around it;
  unrelated tool calls (Read/grep/Edit) are dropped. Calls whose result carries
  a real failure marker (traceback, `Exit code 2/3`, CDPError, no-session
  refusal, connection-refused, …) are tagged **⚠️ FRICTION**. ~50× smaller than raw.
- `manifest.json` — every session with invocation/friction counts and the source
  path, **ranked by friction** (start at the top).
- `raw/` — full jsonl copies, only with `--keep-raw`.

`inbox/` and `.state.json` are gitignored: transcripts contain arbitrary project
content and must not be committed.

## Feeding a batch to Claude

Open a Claude Code session **in this repo** and point it at a batch, e.g.:

> Review the friction in `evals/feedback/inbox/<runid>/`. Start with the
> top sessions in `manifest.json`, read their `distilled/*.md`, and for each
> ⚠️ FRICTION cluster propose a concrete fix to `skill/SKILL.md`, the CLI, or
> `docs/improvements.md`.

Because the distilled `.md` already strips noise and flags the failures, Claude
spends its context on the rough edges, not on scrolling page dumps.
