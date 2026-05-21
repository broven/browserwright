"""Two-tier scorer for the skills-eval harness.

Tier 1 — ``test_patterns``: a deterministic, zero-cost regex gate. This is the
gate that decides pass/fail. Patterns are case-insensitive and DOTALL, and use
alternation so a case passes for any acceptable command-choice variant.

Tier 2 — ``run_llm_judge``: an OPTIONAL 1-5 rubric score from a fixed strong
model (codex). It never decides pass/fail (the deterministic gate owns that);
it only adds a quality signal behind the ``--judge`` flag, and degrades
gracefully to ``None`` when no CLI/credentials are available.
"""

import json
import re
import shutil
import subprocess


def test_patterns(text, expected, forbidden=None):
    """Deterministic gate.

    Returns dict: {passed: bool, missing: [expected regexes that did NOT match],
    hit_forbidden: [forbidden regexes that DID match]}.
    """
    flags = re.IGNORECASE | re.DOTALL | re.MULTILINE
    missing = [p for p in expected if not re.search(p, text, flags)]
    hit_forbidden = [
        p for p in (forbidden or []) if re.search(p, text, flags)
    ]
    return {
        "passed": not missing and not hit_forbidden,
        "missing": missing,
        "hit_forbidden": hit_forbidden,
    }


_JUDGE_PROMPT = """You are scoring an AI agent's reply to a browser-automation task.
The agent has our "browser-skill" loaded (a raw-CDP browser CLI driven by Python
heredocs: new_tab/open_background to navigate, capture_screenshot/snapshot to
perceive, click_at_xy to click; NOT Playwright/Puppeteer, no selector click()).

Score 1-5 by this rubric:
{rubric}

Agent reply:
<reply>
{reply}
</reply>

Respond with ONLY a JSON object, no prose, no fences:
{{"score": <1-5>, "reasoning": "<one short sentence>"}}"""

# Optional judge always uses a fixed strong model so scores are comparable
# across runs regardless of which model produced the reply under test.
_JUDGE_MODEL = "gpt-5-codex"


def run_llm_judge(text, rubric, timeout=120):
    """Optional rubric judge. Returns {score: 1..5, reasoning: str} or None.

    Degrades to None (never raises) when codex is unavailable or output is
    unparseable, so the harness stays usable with no credentials.
    """
    if not rubric:
        return None
    codex = shutil.which("codex")
    if not codex:
        return {"score": None, "reasoning": "judge skipped: codex not on PATH"}

    prompt = _JUDGE_PROMPT.format(rubric=rubric, reply=text)
    try:
        proc = subprocess.run(
            [codex, "exec", "--json", "--skip-git-repo-check",
             "--dangerously-bypass-approvals-and-sandbox",
             "-m", _JUDGE_MODEL, prompt],
            capture_output=True, text=True, timeout=timeout,
        )
    except (subprocess.TimeoutExpired, OSError) as e:
        return {"score": None, "reasoning": f"judge error: {e}"}

    out = _extract_codex_text(proc.stdout)
    return _parse_judge_json(out)


def _parse_judge_json(out):
    # Pull the first {...} object out of the text, tolerate fences/prose.
    m = re.search(r"\{.*\}", out, re.DOTALL)
    if not m:
        return {"score": None, "reasoning": f"unparseable judge output: {out[:160]}"}
    try:
        parsed = json.loads(m.group(0))
        score = parsed.get("score")
        if isinstance(score, (int, float)):
            score = max(1, min(5, int(round(score))))
        return {"score": score, "reasoning": str(parsed.get("reasoning", ""))}
    except (ValueError, TypeError) as e:
        return {"score": None, "reasoning": f"judge parse error: {e}"}


def _extract_codex_text(stdout):
    """codex exec --json emits JSONL; pull the final agent message text."""
    parts = []
    for line in stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        # codex shapes vary across versions; collect any plain message text.
        item = obj.get("item") if isinstance(obj, dict) else None
        if isinstance(item, dict) and item.get("type") == "agent_message":
            t = item.get("text")
            if t:
                parts.append(t)
        elif isinstance(obj, dict) and obj.get("type") == "agent_message":
            t = obj.get("text") or obj.get("message")
            if t:
                parts.append(t)
    return "\n".join(parts).strip() or stdout.strip()
