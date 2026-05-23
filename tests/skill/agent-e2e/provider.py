"""promptfoo custom provider: runs a Claude sub-agent via claude-agent-sdk.

Referenced in promptfooconfig.yaml as:
  providers:
    - id: "file://provider.py"
      config:
        model: "claude-sonnet-4-6"
        max_turns: 25
"""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

from agent_runner import run_agent
from hooks import WORKSPACE_ROOT, EXT_PORT, DAEMON_NAME, ARTIFACTS_DIR


def _build_env() -> dict[str, str]:
    """Build environment for the sub-agent's browserwright calls."""
    return {
        "BS_HOME": str(WORKSPACE_ROOT / ".browserwright"),
        "BD_NAME": DAEMON_NAME,
        "BD_EXTENSION_PORT": str(EXT_PORT),
        "BD_BACKEND": "extension",
        "no_proxy": "127.0.0.1,localhost",
        "NO_PROXY": "127.0.0.1,localhost",
    }


def call_api(prompt: str, options: dict, context: dict) -> dict:
    """promptfoo provider entry point."""
    config = options.get("config", {})
    model = config.get("model", "claude-sonnet-4-6")
    max_turns = config.get("max_turns", 25)

    # user_replies can come from provider config or test vars (as JSON string)
    user_replies = config.get("user_replies", None)
    if user_replies is None:
        vars_ = context.get("vars", {})
        replies_str = vars_.get("user_replies")
        if replies_str:
            user_replies = json.loads(replies_str)

    env = _build_env()

    result = asyncio.run(run_agent(
        prompt,
        workspace=WORKSPACE_ROOT,
        env=env,
        model=model,
        max_turns=max_turns,
        user_replies=user_replies,
    ))

    # Dump trace for debugging
    ARTIFACTS_DIR.mkdir(parents=True, exist_ok=True)
    trace_path = ARTIFACTS_DIR / "last_trace.json"
    trace_path.write_text(json.dumps(result.trace, indent=2, default=str))

    return {
        "output": result.output,
        "tokenUsage": result.usage or {},
        "metadata": {
            "trace": result.trace,
            "turns": result.turns,
            "asked_user": result.asked_user,
            "user_questions": result.user_questions,
            "failed_bash": result.failed_bash,
            "is_error": result.is_error,
            "stop_reason": result.stop_reason,
        },
    }
