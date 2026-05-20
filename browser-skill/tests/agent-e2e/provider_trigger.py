"""Lightweight provider for Case E: skill auto-triggering.

NO daemon or Chrome needed. System prompt lists the real browser-skill
description + distractor skill descriptions. The agent picks which
skill fits the task.
"""
from __future__ import annotations

import asyncio
import os
import re
from pathlib import Path

from claude_agent_sdk import ClaudeSDKClient
from claude_agent_sdk.types import (
    AssistantMessage,
    ClaudeAgentOptions,
    ResultMessage,
    TextBlock,
)

# Read the real browser-skill description from SKILL.md frontmatter
_SKILL_DIR = Path(__file__).resolve().parents[3] / "skill"
_SKILL_MD = _SKILL_DIR / "SKILL.md"


def _extract_description() -> str:
    """Extract the description field from SKILL.md frontmatter."""
    text = _SKILL_MD.read_text(encoding="utf-8")
    m = re.search(r"^description:\s*(.+)$", text, re.MULTILINE)
    return m.group(1).strip() if m else "browser automation CLI"


BROWSER_SKILL_DESC = _extract_description()

DISTRACTOR_SKILLS = [
    {
        "name": "find-domain",
        "description": "Help users find available domain names for products/projects. "
                       "Walks through TLD selection, naming directions, and RDAP-verified availability.",
    },
    {
        "name": "context7",
        "description": "Fetch current documentation for libraries, frameworks, SDKs, "
                       "APIs, CLI tools, or cloud services. Use for API syntax, configuration, "
                       "version migration, setup instructions.",
    },
    {
        "name": "frontend-design",
        "description": "Create distinctive, production-grade frontend interfaces with "
                       "high design quality. Build web components, pages, dashboards, "
                       "React components, HTML/CSS layouts.",
    },
]

SYSTEM_PROMPT = """You are a skill-routing agent. Given a user task, decide which skill best fits.

Available skills:

1. **browser-skill**: {browser_desc}
2. **find-domain**: {find_domain_desc}
3. **context7**: {context7_desc}
4. **frontend-design**: {frontend_desc}

Reply with ONLY the skill name (e.g. "browser-skill") on the first line, then a brief reason on the second line.
""".format(
    browser_desc=BROWSER_SKILL_DESC,
    find_domain_desc=DISTRACTOR_SKILLS[0]["description"],
    context7_desc=DISTRACTOR_SKILLS[1]["description"],
    frontend_desc=DISTRACTOR_SKILLS[2]["description"],
)


async def _run_trigger(task: str, model: str) -> str:
    options = ClaudeAgentOptions(
        model=model,
        system_prompt=SYSTEM_PROMPT,
        tools=[],
        max_turns=1,
        permission_mode="bypassPermissions",
        setting_sources=[],
    )
    output = ""
    async with ClaudeSDKClient(options) as client:
        await client.query(task)
        async for msg in client.receive_response():
            if isinstance(msg, ResultMessage):
                output = msg.result or ""
            elif isinstance(msg, AssistantMessage):
                for b in (msg.content or []):
                    if isinstance(b, TextBlock):
                        output = b.text
    return output


def call_api(prompt: str, options: dict, context: dict) -> dict:
    """promptfoo provider entry point."""
    config = options.get("config", {})
    model = config.get("model", "claude-sonnet-4-6")

    output = asyncio.run(_run_trigger(prompt, model))

    return {
        "output": output,
        "metadata": {
            "chose_browser_skill": "browser-skill" in output.lower(),
        },
    }
