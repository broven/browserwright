"""Scorer for Case E: skill auto-triggering (recall only).

Checks that the agent chose browser-skill for tasks that should trigger it,
even when the user didn't explicitly say "browser-skill".
"""
from __future__ import annotations


def get_assert(output: str, context: dict) -> dict:
    """promptfoo assertion entry point."""
    chose = "browser-skill" in output.lower()
    return {
        "pass": chose,
        "score": 1.0 if chose else 0.0,
        "reason": (
            "correctly chose browser-skill"
            if chose
            else f"did NOT choose browser-skill; output: {output[:200]}"
        ),
    }
