# Reporting browserwright bugs

When browserwright itself misbehaves while you are working, you are the best-placed
reporter: you have the exact commands, the environment, and a live repro in front
of you. File a bug against the upstream repo so a maintainer can reproduce and fix
it — the user should not have to translate the failure into a report themselves.

Upstream repo: **`broven/browserwright`** (GitHub).

**The bar for a valid issue is one thing: a maintainer can reproduce it in a clean
checkout of this repo from your report alone.** If it is not reproducible, it is
not actionable — spend your effort shrinking it to a runnable case, not on prose.

---

## File an issue only for a browserwright defect

Report a bug when browserwright violates its own documented contract:

- A primitive, CLI verb, the daemon, the extension backend, or the Playwright
  facade **crashes, hangs, times out, or returns a wrong result** vs. what
  `browserwright --print-skill` says it should do.
- A tool traceback surfaces from browserwright's own code (not your script's
  logic error).
- Documented behavior (e.g. `page.goto()` smart-waiting, `snapshot()` refs,
  `state` persistence, session rebind after `reset()`) does not hold.

**Do NOT file an issue for:**

- **Website behavior** — the site changed its markup, blocks automation, shows a
  captcha/paywall/rate-limit, or a selector went stale. That is a site note:
  `remember(host, ...)`, not a browserwright bug.
- **Prompt injection or hostile page content.** See `trust-boundaries.md`.
- **Your own script logic**, a bad selector, or a wrong argument you passed.
- **Transient, one-off flakiness you cannot reproduce.** See the repro bar below.
- **Something already reported.** First run
  `gh issue list --repo broven/browserwright --search "<keywords>" --state all`
  and skip if it already exists (mention the number to the user instead).

If you are unsure whether it is a browserwright bug or a site quirk, try to
reproduce it against a neutral page (e.g. `data:text/html,...` or
`https://example.com`). If the neutral page also fails, it is a browserwright bug.

---

## Before you file: make it reproducible

1. **Reproduce it at least twice.** Run the failing step again. If it does not
   recur, it is flakiness — do not file unless it is severe (data loss, crash
   loop) and you can attach logs; say "intermittent, N/M runs" if so.

2. **Minimize to the smallest runnable case.** Cut everything not needed to
   trigger the failure. The goal is a script a maintainer can paste and run:
   - Prefer a **public, stable URL** the maintainer can reach: `https://example.com`,
     a specific Wikipedia article, or a self-contained `data:text/html,<...>` URL.
   - If the trigger needs specific markup, embed it in a `data:` URL or a tiny
     HTML fixture so the repro carries its own page.
   - **Avoid auth-gated, private, or internal pages** — a maintainer cannot open
     them, so the report is not reproducible. Recreate the triggering condition
     on a page they can reach.

3. **Capture the environment** (paste verbatim into the issue):

   ```bash
   browserwright version check
   browserwright-daemon status --json
   ```

   Note the backend you used (`extension` / `rdp` / `env`) and the OS.

4. **Capture the failure exactly:** the full error text / traceback, plus a clear
   *expected vs. actual*.

5. **Redact before sending.** Strip secrets, tokens, cookies, session ids,
   personal data, and private page content. Replace with placeholders like
   `<REDACTED_TOKEN>`. A GitHub issue is public. Never paste anything from
   `trust-boundaries.md`'s "content channel" that could carry sensitive data.

If you cannot get a page a maintainer can reach to fail, you do not have a
reproducible issue yet — keep minimizing or record it as a site note instead.

---

## Issue format

Title: a concise symptom, prefixed so agent-filed reports are easy to triage:

```
[agent-bug] <one-line symptom> (<backend> backend)
```

Body — use these sections verbatim:

```markdown
## Summary
<one or two sentences: what broke>

## Reproduction
Minimal, runnable steps. A maintainer must be able to paste and run this.

```bash
sid=$(browserwright session new --backend=rdp --create --name=repro)
browserwright -s "$sid" -e $'
page.goto("https://example.com")
# ... the smallest sequence that triggers the bug ...
'
browserwright session end --session=$sid
```

## Expected
<what the docs / primitive contract say should happen>

## Actual
<what actually happened — paste the full error/traceback in a code block>

## Environment
<paste of `browserwright version check`>
<paste of `browserwright-daemon status --json`>
- Backend: extension | rdp | env
- OS: <e.g. macOS 15.1>

## Notes
<anything else: frequency if intermittent, workaround found, related refs>

---
Reported by a browserwright code agent on the user's behalf.
```

Prefer `--backend=rdp --create` in the reproduction when the bug does not
require the user's own Chrome: an isolated Chrome is the environment a maintainer
can reproduce most easily. Only use the `extension` backend in the repro if the
bug is specific to it, and say so.

---

## Filing it

A GitHub issue is public and is created under the user's `gh` identity, so this
is outward-facing: **draft the full issue, show it to the user, and file it only
after they approve.** That still removes the burden of writing the report.

Write the body to a temp file (avoids shell-quoting mangling), then create it:

```bash
gh issue create \
  --repo broven/browserwright \
  --title "[agent-bug] page.goto hangs on cross-site iframe (rdp backend)" \
  --body-file /tmp/browserwright-issue.md
```

Add `--label bug` (and `--label agent-reported` if the repo has that label). If
`gh` rejects a label that does not exist, drop the flag and retry — the
"Reported by a browserwright code agent" line in the body is the durable marker.

**Fallback when `gh` is missing or unauthenticated:** save the drafted issue to
`~/.browserwright/issues/<short-slug>.md`, tell the user the file path, and give
them the exact `gh issue create --body-file ...` command (or the repo's "New
issue" URL) to submit it themselves. Do not silently drop the report.

After filing, tell the user the issue number/URL so they can track the fix.
