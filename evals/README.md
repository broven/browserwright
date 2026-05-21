# skills-eval (browser-skill)

A tiny, text-level harness that measures whether **our `skill/SKILL.md` actually
steers a model to the right command *choice***. It feeds

    [SKILL.md content] + [optional "you already loaded the skill" preamble] + [a task prompt]

to a real agent CLI, captures the agent's **text reply** (the commands/plan it
emits), and **scores the text** — it never launches a real browser.

That cheapness is the whole point. This is complementary to the heavyweight live
e2e suites (`browser-daemon/tests/e2e`, `browser-skill/tests/agent-e2e`, which
drive a real Chrome): this harness is deterministic, fast, and free in `--mock`
mode, so every SKILL.md edit gets a red/green signal before you pay for a live
run. It operationalizes the team's
**"skill as spec: run red → fix the skill to green, but never overfit"**
discipline (improvements.md item E1).

## Files

| file | purpose |
| --- | --- |
| `run.py` | runner: builds prompt, calls a pluggable agent runner, scores, reports, exits 1 on any fail |
| `cases.py` | the eval cases (dicts) across three categories + rubrics + the "loaded-skill" stub preamble |
| `judge.py` | two-tier scorer: `test_patterns` (deterministic gate) + optional `run_llm_judge` (1-5) |
| `mock_transcripts.py` | canned good+bad agent replies so the harness is self-testable at zero token cost |
| `README.md` | this file |

## Run it

```bash
# Zero-cost self-test: GOOD canned transcripts -> all PASS, exit 0
python3 evals/run.py --mock

# Zero-cost self-test: BAD canned transcripts -> all FAIL, exit 1 (proves the gate bites)
python3 evals/run.py --mock-bad

# Real run against an agent CLI (default runner: codex)
python3 evals/run.py                     # all cases
python3 evals/run.py --case cu-01        # one case (cheapest real proof)
python3 evals/run.py --category command-usage
python3 evals/run.py --judge             # add an optional 1-5 rubric score
python3 evals/run.py --json              # machine-readable; exit 1 on fail (CI)
```

- **Runner** is pluggable. `--mock` / `--mock-bad` use the canned banks in
  `mock_transcripts.py`. The real runner shells out to `codex exec` (the prompt
  is piped on stdin via the `-` sentinel). `claude` is only a shell function in
  this environment, so `codex` is the default real runner.
- **Exit code** is 1 if any case fails or errors — drop `python3 evals/run.py
  --json` straight into CI.

## The two-tier score

1. **Deterministic gate (`test_patterns`)** — owns pass/fail. Every
   `expected_patterns` regex must match and no `forbidden_patterns` regex may
   match (case-insensitive, DOTALL, multiline).
2. **Optional LLM judge (`run_llm_judge`, behind `--judge`)** — a 1-5 rubric
   score from a fixed strong model (codex). It is a *quality signal only*; it
   never flips pass/fail. With no CLI/credentials it degrades gracefully to a
   `score: None` note instead of erroring.

## Categories

- **`skill-loading`** — does the agent reach for `browser-skill` (and a
  non-clobbering nav) before acting on a page?
- **`skill-selection`** — does it pick the browser when (and only when) the
  browser is the right tool? Includes a *distractor* case (a pure docs lookup)
  where driving the browser is the wrong answer.
- **`command-usage`** — given the skill is loaded, does it choose the right
  primitives: `new_tab`/`open_background` (not `goto_url`) for the first nav,
  `snapshot`/`capture_screenshot` then `click_at_xy` to click, `http_get` for
  bulk static fetches — and avoid `playwright`/`puppeteer`/selector-`click()`?

## Adding a case

Append a dict to `CASES` in `cases.py`:

```python
{
    "id": "cu-04",
    "name": "short label",
    "category": "command-usage",            # or skill-loading / skill-selection
    "prompt": "the user task",
    "context": LOADED_CONTEXT,              # optional: test command choice in isolation
    "expected_patterns": [ r"\b(new_tab|open_background)\s*\(" ],
    "forbidden_patterns": [ r"^\s*goto_url\s*\(" ],   # optional
    "rubric": COMMAND_RUBRIC,               # optional, for --judge
}
```

Then add a GOOD and a BAD canned transcript for the new id to
`mock_transcripts.py` so `--mock` / `--mock-bad` keep proving the gate in both
directions.

## Anti-overfit rule (the team cares about this)

**Assert command-CHOICE behavior via variant alternations and forbidden
patterns — never exact phrasing.** A case that only passes one specific wording
is a bug.

- Use alternation for any acceptable variant: `(new_tab|open_background)`,
  `(capture_screenshot|screenshot)`, `(snapshot|capture_screenshot)`.
- Encode the *wrong* behaviors the skill should suppress as
  `forbidden_patterns`: `goto_url` as the first nav (clobbers the user's tab),
  `playwright|puppeteer|selenium`, a selector-based `click("…")` API we don't
  have.
- The canned transcripts in `mock_transcripts.py` are deliberately worded
  **differently** from the cases (different variable names, comments, ordering).
  If a case only passes because it memorized the transcript's phrasing, it will
  fail the mock — that's the guard against overfitting.
- When the skill changes a recommended command, update the *variant set*, not a
  single literal string.

## Honest limitations

- **Text-scoring blind spots.** We score the commands the agent *says* it would
  run, not their runtime effect. An agent can emit perfect-looking commands that
  would fail live (wrong coordinates, stale tab, race). That is what the live
  e2e suites are for; this harness gates *command choice*, not *execution*.
- **Judge cost & variance.** `--judge` spends real tokens and a strong model can
  still mis-score borderline replies; treat the 1-5 as a soft signal, not a
  gate.
- **Real-runner flakiness.** The real run depends on the upstream model gateway;
  transient `503`s show up as an empty response → FAIL. Re-run, or rely on the
  deterministic `--mock` proof for CI. Keep real runs to one or a few cases.
- **Not a substitute for live.** Use this for fast iteration on SKILL.md
  steering; use the live e2e suites (`browser-daemon/tests/e2e`,
  `browser-skill/tests/agent-e2e`) to confirm the commands actually drive a
  browser.
```
