"""YAML for memory frontmatter — a thin wrapper over PyYAML.

Frontmatter blocks (spec §C.2) are plain mappings: scalars, lists, nested
dicts, ISO dates as strings. ``loads`` / ``dumps`` keep the historical
hand-rolled signatures so ``_md.py`` callers don't change:

  - ``loads`` always returns a dict (empty/non-mapping input → ``{}``).
  - ``dumps`` emits block style, preserves key order, keeps unicode.
"""
from __future__ import annotations

import datetime as _dt
from typing import Any

import yaml


def _dates_to_str(value: Any) -> Any:
    """Normalise YAML date/datetime nodes back to ISO strings.

    The previous hand-rolled parser kept ``last_updated: 2026-07-02`` as a
    plain string; PyYAML resolves it to ``datetime.date``. Callers (and the
    JSON output layer) expect strings, so we keep that contract.
    """
    if isinstance(value, (_dt.datetime, _dt.date)):
        return value.isoformat()
    if isinstance(value, dict):
        return {k: _dates_to_str(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_dates_to_str(v) for v in value]
    return value


def loads(text: str) -> dict[str, Any]:
    """Parse a YAML frontmatter block into a dict.

    Non-mapping documents (empty, scalar, list) return ``{}`` — frontmatter
    is a mapping by contract, and callers treat "no usable frontmatter" as
    an empty dict.
    """
    data = yaml.safe_load(text)
    return _dates_to_str(data) if isinstance(data, dict) else {}


def dumps(data: dict[str, Any]) -> str:
    """Render ``data`` as block-style YAML (insertion order kept)."""
    if not data:
        return ""
    return yaml.safe_dump(
        data,
        sort_keys=False,
        allow_unicode=True,
        default_flow_style=False,
    )
