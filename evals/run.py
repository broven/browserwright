#!/usr/bin/env python3
"""skills-eval runner for browserwright (improvements.md item E1).

Feeds [skill content + a "you have browserwright loaded" preamble] + [a task
prompt] to an agent runner, captures the agent's TEXT reply, and scores it with
the two-tier gate in judge.py. It does NOT launch a real browser — that cheap,
deterministic text scoring is the point, complementary to the live e2e suites
(browserwright-daemon/tests/e2e, browserwright/tests/agent-e2e).

Usage:
    python3 evals/run.py                       # real run via codex (default)
    python3 evals/run.py --mock                # zero-cost canned transcripts
    python3 evals/run.py --category command-usage
    python3 evals/run.py --judge               # add optional 1-5 rubric score
    python3 evals/run.py --json                # machine-readable; exit 1 on fail
    python3 evals/run.py --case cu-01          # one case (for the real proof run)

Exit code is 1 if any case fails or errors (CI-ready).
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from cases import CASES  # noqa: E402
from judge import test_patterns, run_llm_judge  # noqa: E402
from mock_transcripts import MOCK_GOOD, MOCK_BAD  # noqa: E402

SKILL_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "skill", "SKILL.md",
)

_GREEN, _RED, _YELLOW, _DIM, _BOLD, _RESET = (
    "\033[32m", "\033[31m", "\033[33m", "\033[2m", "\033[1m", "\033[0m",
)


def build_prompt(case, skill_text):
    """skill-context preamble + optional per-case stub + the task prompt."""
    parts = [
        "You have the following skill installed and active:\n",
        "<skill>", skill_text, "</skill>\n",
    ]
    if case.get("context"):
        parts.append(case["context"] + "\n")
    parts.append("Task: " + case["prompt"] + "\n")
    parts.append(
        "Show the exact shell commands (browserwright heredocs) you would run. "
        "Be concise; commands over prose."
    )
    return "\n".join(parts)


# --- pluggable agent runners ------------------------------------------------

def make_mock_runner(bank):
    """Build a mock runner over a transcript bank keyed by case id."""
    def _runner(case, _prompt):
        if case["id"] not in bank:
            return "", f"no mock transcript for case {case['id']}"
        return bank[case["id"]], None
    return _runner


def codex_runner(case, prompt, timeout=180):
    """Real runner: shell out to `codex exec`. Returns (text, error)."""
    codex = shutil.which("codex")
    if not codex:
        return "", "codex not found on PATH"
    try:
        # Pass the (long) prompt on stdin via the `-` sentinel — more robust
        # than a giant argv string.
        proc = subprocess.run(
            [codex, "exec", "--json", "--skip-git-repo-check",
             "--dangerously-bypass-approvals-and-sandbox", "-"],
            input=prompt, capture_output=True, text=True, timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return "", f"codex timed out after {timeout}s"
    except OSError as e:
        return "", f"codex spawn error: {e}"
    if proc.returncode != 0:
        return "", f"codex exit {proc.returncode}: {proc.stderr[:200]}"
    from judge import _extract_codex_text
    return _extract_codex_text(proc.stdout), None


RUNNERS = {"codex": codex_runner}


# --- evaluation -------------------------------------------------------------

def evaluate(case, runner, skill_text, want_judge):
    prompt = build_prompt(case, skill_text)
    t0 = time.time()
    text, err = runner(case, prompt)
    dur = round((time.time() - t0) * 1000)
    if err:
        return {"id": case["id"], "name": case["name"],
                "category": case["category"], "passed": False,
                "missing": [], "hit_forbidden": [], "judge": None,
                "response": text, "duration_ms": dur, "error": err}

    gate = test_patterns(text, case["expected_patterns"],
                         case.get("forbidden_patterns"))
    judge = None
    if want_judge:
        judge = run_llm_judge(text, case.get("rubric"))
    return {"id": case["id"], "name": case["name"],
            "category": case["category"], "passed": gate["passed"],
            "missing": gate["missing"], "hit_forbidden": gate["hit_forbidden"],
            "judge": judge, "response": text, "duration_ms": dur, "error": None}


# --- reporting --------------------------------------------------------------

def print_result(r):
    if r["error"]:
        icon, status = _YELLOW + "!" + _RESET, "ERROR"
    elif r["passed"]:
        icon, status = _GREEN + "✓" + _RESET, "PASS"
    else:
        icon, status = _RED + "✗" + _RESET, "FAIL"
    print(f"  {icon} {r['name']:<48} {status}  {_DIM}{r['duration_ms']}ms{_RESET}")
    if r["error"]:
        print(f"    {_DIM}error: {r['error']}{_RESET}")
        return
    for p in r["missing"]:
        print(f"    {_RED}✗{_RESET} expected not found: {_DIM}{p}{_RESET}")
    for p in r["hit_forbidden"]:
        print(f"    {_RED}✗{_RESET} forbidden matched:  {_DIM}{p}{_RESET}")
    if r["judge"] and r["judge"].get("score") is not None:
        print(f"    {_DIM}judge: {r['judge']['score']}/5 - {r['judge']['reasoning']}{_RESET}")
    elif r["judge"]:
        print(f"    {_DIM}judge: {r['judge']['reasoning']}{_RESET}")


def main():
    ap = argparse.ArgumentParser(description="browserwright skills-eval harness")
    ap.add_argument("--mock", action="store_true",
                    help="use the GOOD canned transcripts (zero token cost)")
    ap.add_argument("--mock-bad", action="store_true",
                    help="use the BAD canned transcripts to prove the gate fails them")
    ap.add_argument("--runner", default="codex", choices=list(RUNNERS),
                    help="real agent runner (ignored when --mock/--mock-bad)")
    ap.add_argument("--category",
                    choices=["skill-loading", "skill-selection", "command-usage"])
    ap.add_argument("--case", help="run a single case by id")
    ap.add_argument("--judge", action="store_true",
                    help="add optional 1-5 LLM rubric score (extra cost)")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    args = ap.parse_args()

    if args.mock_bad:
        runner = make_mock_runner(MOCK_BAD)
    elif args.mock:
        runner = make_mock_runner(MOCK_GOOD)
    else:
        runner = RUNNERS[args.runner]
    try:
        with open(SKILL_PATH, encoding="utf-8") as f:
            skill_text = f.read()
    except OSError as e:
        print(f"cannot read skill at {SKILL_PATH}: {e}", file=sys.stderr)
        return 2

    selected = CASES
    if args.category:
        selected = [c for c in selected if c["category"] == args.category]
    if args.case:
        selected = [c for c in selected if c["id"] == args.case]
    if not selected:
        print("no cases match the given filters", file=sys.stderr)
        return 1

    if not args.json:
        mode = ("mock-bad" if args.mock_bad
                else "mock" if args.mock else args.runner)
        print(f"\nrunning {len(selected)} case(s) via {mode}"
              + (" + judge" if args.judge else ""))

    results, cur_cat = [], None
    for case in selected:
        if not args.json and case["category"] != cur_cat:
            cur_cat = case["category"]
            print(f"\n{_BOLD}{cur_cat}{_RESET}\n" + "-" * 60)
        r = evaluate(case, runner, skill_text, args.judge)
        results.append(r)
        if not args.json:
            print_result(r)

    passed = sum(1 for r in results if r["passed"] and not r["error"])
    failed = sum(1 for r in results if not r["passed"] and not r["error"])
    errors = sum(1 for r in results if r["error"])

    if args.json:
        print(json.dumps({
            "summary": {"total": len(results), "passed": passed,
                        "failed": failed, "errors": errors},
            "results": results,
        }, indent=2))
    else:
        print(f"\n{_BOLD}summary{_RESET}\n" + "=" * 60)
        ok = failed == 0 and errors == 0
        icon = (_GREEN + "✓" if ok else _RED + "✗") + _RESET
        print(f"  {icon} {passed}/{len(results)} passed"
              + (f", {failed} failed" if failed else "")
              + (f", {errors} errors" if errors else ""))

    return 0 if (failed == 0 and errors == 0) else 1


if __name__ == "__main__":
    sys.exit(main())
