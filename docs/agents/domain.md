# Domain Docs

How the engineering skills should consume this repo's domain documentation when exploring the codebase.

## Before exploring, read these

- **`CONTEXT.md`** at the repo root
- **`docs/adr/`** — read ADRs that touch the area you're about to work in

If either doesn't exist, **proceed silently**. Don't flag its absence; don't suggest creating it upfront. The `/domain-modeling` skill (reached via `/grill-with-docs` and `/improve-codebase-architecture`) creates them lazily when terms or decisions actually get resolved.

## File structure

This repo is **single-context** — one `CONTEXT.md` and one `docs/adr/`, both at the root:

```
/
├── CONTEXT.md
├── docs/adr/
│   ├── 0001-<slug>.md
│   └── 0002-<slug>.md
└── src/browserwright/
```

There is no `CONTEXT-MAP.md`. If one ever appears at the root, the repo has split into multiple contexts and each gets its own `CONTEXT.md` + `docs/adr/`.

## What lives where

`CONTEXT.md` **names** things — one term, one meaning, one trap. It deliberately does not hold rules. The session model's *rules* (invariants, ownership, teardown semantics) live in [`docs/session-workspaces.md`](../session-workspaces.md); read that before changing session routing, backend semantics, tab creation, facade behavior, or teardown. Architecture vocabulary (module, interface, depth, seam, adapter) belongs to the `/codebase-design` skill, not to `CONTEXT.md`.

When `CONTEXT.md` and the code disagree, one of them is a bug — fix one, don't invent a third word.

## ADRs are written in Chinese

**Write every ADR in this repo in Chinese (中文).** The maintainer reviews them
in Chinese; an English ADR will be rewritten. Three things stay in English:

- the **filename slug** (`0004-agent-chooses-backend.md`) — it is a path and a
  numbering anchor;
- **code identifiers** verbatim — `backend != "extension"`, `_raw_cdp_backend`,
  `Target.setAutoAttach`, `daemon.preferred_backend`, file paths, `cdp` /
  `extension` as literal backend values;
- **links and citations** to code and external projects.

This applies to ADRs only. `CONTEXT.md` and the rest of `docs/` stay in English.

## Use the glossary's vocabulary

When your output names a domain concept (in an issue title, a refactor proposal, a hypothesis, a test name), use the term as defined in `CONTEXT.md`. Don't drift to synonyms the glossary explicitly avoids — the *Retired* table at the bottom of `CONTEXT.md` lists words that are gone on purpose.

If the concept you need isn't in the glossary yet, that's a signal — either you're inventing language the project doesn't use (reconsider) or there's a real gap (note it for `/domain-modeling`).

## Flag ADR conflicts

If your output contradicts an existing ADR, surface it explicitly rather than silently overriding:

> _Contradicts ADR-0007 (event-sourced orders) — but worth reopening because…_
