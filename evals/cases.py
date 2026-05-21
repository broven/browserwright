"""Eval cases for the browser-skill skills-eval harness.

Each case asserts COMMAND-CHOICE BEHAVIOR, never exact phrasing. Patterns use
alternation (``new_tab|open_background``) so a case passes for any acceptable
wording the model emits, and ``forbidden`` patterns catch the wrong-tool reflexes
our SKILL.md is supposed to steer away from.

Schema (plain dicts, stdlib-only):
    id                 short stable id
    name               human label
    category           "skill-loading" | "skill-selection" | "command-usage"
    prompt             the user task sent to the agent
    context            optional preamble injected AFTER the skill content
                       (e.g. a "you already loaded browser-skill" stub) so a
                       case can test command *choice* in isolation
    expected_patterns  regexes that must ALL match (case-insensitive, DOTALL)
    forbidden_patterns regexes that must NOT match
    rubric             optional 1-5 rubric for the --judge pass
"""

# ---------------------------------------------------------------------------
# Rubric encodes our workflow doctrine for the optional LLM judge. It mirrors
# the deterministic gate but lets a judge reward *good* workflow ordering.
# ---------------------------------------------------------------------------
COMMAND_RUBRIC = """
1 - Does not produce valid browser-skill heredoc commands.
2 - Uses browser-skill but with wrong primitives or a clobbering first nav (goto_url).
3 - Correct primitives but skips the screenshot/snapshot-before-interact step.
4 - Correct primitives in the right order (perceive, then act).
5 - Optimal: open_background/new_tab for first nav, snapshot or capture_screenshot
    to locate, click_at_xy on coordinates, re-verify after acting.
""".strip()

LOADING_RUBRIC = """
1 - Ignores the browser entirely / tries to answer without a browser.
2 - Mentions a browser tool vaguely but never invokes browser-skill.
3 - Invokes browser-skill but malformed or without a heredoc.
4 - Invokes the browser-skill heredoc before acting on the page.
5 - Invokes browser-skill and follows the perceive-then-act workflow.
""".strip()

SELECTION_RUBRIC = """
1 - Picks a clearly wrong tool (e.g. plain curl for a JS-driven click flow).
2 - Picks an arguably-related but wrong tool.
3 - Picks the right family but the wrong specific tool.
4 - Picks browser-skill when (and only when) the browser is the right tool.
5 - Picks correctly AND briefly justifies why the browser is needed here.
""".strip()

# A stub preamble that simulates "the agent already loaded browser-skill", so
# command-usage cases test the COMMAND CHOICE in isolation (not whether the
# agent remembered to load the skill — that is skill-loading's job).
LOADED_CONTEXT = """
You already have the browser-skill loaded. You drive the browser by piping a
Python heredoc to the `browser-skill` CLI:

    browser-skill <<'PY'
    ... primitives, pre-imported ...
    PY

Primitives available (pre-imported): new_tab(url), goto_url(url),
open_background(url), attach_active(), switch_tab(id), click_at_xy(x, y),
type_text(s), capture_screenshot(), snapshot(), page_info(), js(expr),
wait_for_load().

Reminder of the rules: the FIRST navigation must use new_tab() or
open_background() — never goto_url(), which runs in the user's active tab and
clobbers their work. To click, first capture_screenshot() or snapshot() to find
the target, then click_at_xy(x, y) on the coordinates. There is no selector-based
click(); this is raw CDP, not Playwright/Puppeteer.
""".strip()

CASES = [
    # ---- command-usage -----------------------------------------------------
    {
        "id": "cu-01",
        "name": "Open + screenshot uses non-clobbering nav",
        "category": "command-usage",
        "prompt": "Open example.com and take a screenshot of it.",
        "context": LOADED_CONTEXT,
        "expected_patterns": [
            r"\bbrowser-skill\b",
            r"\b(new_tab|open_background)\s*\(",
            r"\b(capture_screenshot|screenshot)\b",
        ],
        "forbidden_patterns": [
            # goto_url as the FIRST navigation clobbers the user's tab.
            r"^\s*goto_url\s*\(",
            # wrong-tool reflex
            r"\b(playwright|puppeteer|selenium|getBoundingClientRect)\b",
        ],
        "rubric": COMMAND_RUBRIC,
    },
    {
        "id": "cu-02",
        "name": "Click Submit: perceive then click_at_xy",
        "category": "command-usage",
        "prompt": "On the page that is open, click the Submit button.",
        "context": LOADED_CONTEXT,
        "expected_patterns": [
            r"\b(snapshot|capture_screenshot)\s*\(",
            r"\bclick_at_xy\s*\(",
        ],
        "forbidden_patterns": [
            # We have no selector-based click API; inventing one is a failure.
            r"\bclick\s*\(\s*['\"]",
            r"\b(playwright|puppeteer|selenium)\b",
        ],
        "rubric": COMMAND_RUBRIC,
    },
    {
        "id": "cu-03",
        "name": "Bulk static fetch avoids the browser",
        "category": "command-usage",
        "prompt": (
            "Fetch the raw HTML of 50 static URLs like https://example.com/page/1 "
            "through /page/50 as fast as possible. They are plain server-rendered "
            "pages with no JavaScript."
        ),
        "context": LOADED_CONTEXT,
        "expected_patterns": [
            # http_get (no browser) is the right tool for static bulk fetch.
            r"\bhttp_get\b|\b(curl|requests|httpx|urllib|WebFetch)\b",
        ],
        "forbidden_patterns": [
            # Spinning up a real tab per page is the wrong tool here.
            r"\bclick_at_xy\b",
        ],
        "rubric": COMMAND_RUBRIC,
    },
    {
        "id": "cu-04",
        "name": "Stale page: agent reloads it itself, never asks the user",
        "category": "command-usage",
        "prompt": (
            "I'm watching a tab you've been driving. The data on it is stale — "
            "it didn't update after the last action. Get the page to show the "
            "current state."
        ),
        "context": LOADED_CONTEXT,
        "expected_patterns": [
            # The agent should drive the refresh itself with reload().
            r"\breload\s*\(|\bPage\.reload\b",
        ],
        "forbidden_patterns": [
            # Outsourcing a browser action it can do itself is the failure mode
            # this case guards (multi-variant, EN + ZH, anti-overfit).
            r"(ask|asking)\s+you\s+to",
            r"please\s+refresh",
            r"manually\s+refresh",
            r"refresh\s+the\s+page\s+yourself",
            r"你.{0,4}刷新",
            r"手动刷新",
        ],
        "rubric": COMMAND_RUBRIC,
    },
    # ---- skill-loading -----------------------------------------------------
    {
        "id": "sl-01",
        "name": "Loads browser-skill before acting on a page",
        "category": "skill-loading",
        # No LOADED_CONTEXT: the agent must reach for the skill itself.
        "prompt": "Take a screenshot of the front page of news.ycombinator.com.",
        "expected_patterns": [
            r"\bbrowser-skill\b",
            r"\b(new_tab|open_background|attach_active)\s*\(",
        ],
        "forbidden_patterns": [
            r"\b(playwright|puppeteer|selenium)\b",
        ],
        "rubric": LOADING_RUBRIC,
    },
    # ---- skill-selection ---------------------------------------------------
    {
        "id": "ss-01",
        "name": "Picks browser-skill for a JS-driven click flow",
        "category": "skill-selection",
        "prompt": (
            "I need to log into a single-page web app, click through a multi-step "
            "wizard, and submit a form. The buttons only appear after JS runs. "
            "Which tool should you use, and start the flow."
        ),
        "expected_patterns": [
            r"\bbrowser-skill\b",
        ],
        "forbidden_patterns": [
            # A JS-driven click flow is NOT a curl/WebFetch job.
            r"^\s*(curl|wget)\b",
            r"\bWebFetch\b",
        ],
        "rubric": SELECTION_RUBRIC,
    },
    {
        "id": "ss-02",
        "name": "Does NOT reach for the browser on a pure docs lookup",
        "category": "skill-selection",
        # Distractor: browser-skill is the WRONG tool here. The skill's
        # "When NOT to use this skill" section says docs lookups go to context7.
        "prompt": (
            "What is the function signature of React's useEffect hook? "
            "Just answer from the library docs."
        ),
        "expected_patterns": [
            # Should NOT drive a browser for this; should mention a docs path.
            r"\b(context7|docs|documentation|knowledge|signature)\b",
        ],
        "forbidden_patterns": [
            r"\bbrowser-skill\b",
            r"\b(new_tab|open_background|click_at_xy|capture_screenshot)\s*\(",
        ],
        "rubric": SELECTION_RUBRIC,
    },
]
