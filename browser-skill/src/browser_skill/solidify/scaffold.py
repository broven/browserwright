"""Scaffold a new ``tasks/<name>.py`` from a propose_solidify() spec."""
from __future__ import annotations

import datetime as _dt
import re
import textwrap
from pathlib import Path
from typing import Any

from ..memory.site_mem import bootstrap_site, site_dir


_TEMPLATE = '''"""{description}"""
from browser_skill import *  # noqa: F401, F403

ARGS = {args_repr}

OUTPUT = "{output}"

# OUTPUT_SCHEMA (REVIEW.md F-7): optional pydantic / JSON-schema shape
# task_runner uses to validate run()'s return value. Uncomment + fill in
# when run() returns a structured dict / list. The {{...}} dict form is
# the canonical shape — see browser_skill/output_schema.py for accepted
# variants. Leaving this commented out is fine; validation is skipped.
{output_schema_block}

TAGS = {tags_repr}
REQUIRES_LOGIN = {requires_login}
ESTIMATED_DURATION_SEC = {duration}
LAST_VERIFIED = "{today}"


def selftest():
    """Quickly verify the site structure hasn't drifted. Fill in with
    URL-pattern asserts and one-or-two stable selector checks."""
    # TODO(agent): add a navigation + assert here so SiteDrift trips early.
    return True


def run(args, ctx=None):
{run_body}
'''


def _format_output_schema(spec: dict) -> str:
    """Emit either the actual ``OUTPUT_SCHEMA = {...}`` line (when propose
    surfaced a draft schema) or a commented-out template the agent can
    fill in. REVIEW.md F-7."""
    sch = spec.get("draft_output_schema")
    if isinstance(sch, dict) and sch:
        return f"OUTPUT_SCHEMA = {sch!r}"
    # Commented placeholder — agent fills in after first run() return.
    return (
        "# OUTPUT_SCHEMA = {\n"
        "#     \"type\": \"object\",\n"
        "#     \"properties\": {},  # fill in once you see what run() returns\n"
        "#     \"required\": [],\n"
        "# }"
    )


def _safe_name(name: str) -> str:
    name = re.sub(r"[^a-zA-Z0-9_]+", "_", name).strip("_").lower()
    if not name:
        raise ValueError("task name is empty after sanitisation")
    return name


def _validate_args_schema(args: Any) -> None:
    """Spec §B.3 — each value must be a dict with at least a ``type`` field.

    Pre-1.0 agents often flatten the shape to ``{"q": "str"}`` (Bug 2 from
    the v0.3 AI E2E run). The downstream template render then trips a
    confusing ``AttributeError: 'str' object has no attribute 'items'``
    deep inside ``_format_args_dict``. Validate up-front so the agent gets
    a single actionable error instead.
    """
    if not isinstance(args, dict):
        raise ValueError(
            "args schema must be a dict mapping argname → metadata dict "
            f"(got {type(args).__name__}). "
            "Example: {'q': {'type': 'str', 'required': True}}"
        )
    for k, v in args.items():
        if not isinstance(k, str):
            raise ValueError(
                f"args schema malformed: key {k!r} must be a string "
                f"(got {type(k).__name__}). "
                "Example: {'q': {'type': 'str', 'required': True}}"
            )
        if not isinstance(v, dict):
            raise ValueError(
                f"args schema malformed: entry {k!r} must be a dict, got "
                f"{type(v).__name__} ({v!r}). "
                "Example: {'q': {'type': 'str', 'required': True}}"
            )
        if "type" not in v:
            raise ValueError(
                f"args schema malformed: entry {k!r} missing required 'type' "
                f"field (got keys {sorted(v.keys())}). "
                "Example: {'q': {'type': 'str', 'required': True}}"
            )


def _format_args_dict(args: dict[str, dict]) -> str:
    if not args:
        return "{}"
    lines = ["{"]
    for k, meta in args.items():
        # Repr the metadata dict but keep keys ordered for diff stability.
        body = ", ".join(f'"{ik}": {iv!r}' for ik, iv in meta.items())
        lines.append(f'    "{k}": {{{body}}},')
    lines.append("}")
    return "\n".join(lines)


def commit(session, spec: dict[str, Any]) -> dict[str, Any]:
    """Take a propose-shaped dict and write the task file.

    Returns ``{"path": ..., "site": ..., "name": ...}``. Does NOT execute
    selftest — that's the agent's job after review (spec §B.4.3).
    """
    site = spec.get("site") or "unknown"
    name = _safe_name(spec.get("suggested_name") or spec.get("name") or "task")
    # Make sure the site directory exists.
    host_seed = spec.get("host_hint") or site
    bootstrap_site(host_seed)

    body = spec.get("draft_run_body") or "    pass  # agent: fill me in\n"
    if not body.endswith("\n"):
        body += "\n"
    args_schema = spec.get("draft_args_schema") or {}
    _validate_args_schema(args_schema)
    today = _dt.date.today().isoformat()
    rendered = _TEMPLATE.format(
        description=spec.get("description") or f"{name} on {site}",
        args_repr=_format_args_dict(args_schema),
        output=spec.get("output") or "Any",
        output_schema_block=_format_output_schema(spec),
        tags_repr=repr(spec.get("tags") or []),
        requires_login=str(bool(spec.get("requires_login", False))),
        duration=int(spec.get("estimated_duration_sec", 30)),
        today=today,
        run_body=body.rstrip("\n"),
    )

    target_dir = site_dir(host_seed) / "tasks"
    target_dir.mkdir(parents=True, exist_ok=True)
    out_path = target_dir / f"{name}.py"
    out_path.write_text(rendered, encoding="utf-8")
    # Append a Task history line to the site memory.
    try:
        from ..memory import site_memory
        site_memory(host_seed).append(
            f"task `{name}` created on {today}", section="Task history"
        )
    except Exception:
        pass
    return {"path": str(out_path.resolve()), "site": site, "name": name}
