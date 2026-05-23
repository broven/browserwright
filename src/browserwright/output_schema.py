"""Minimal JSON-Schema-subset validator for task ``OUTPUT_SCHEMA`` (v0.2).

We don't pull in ``jsonschema`` — task schemas in practice cover one of
five shapes: list-of-dicts, dict, scalar, optional/nullable, and unions.
The validator handles those plus enough
plumbing for nested ``items`` / ``properties``. If a task needs a richer
schema it can ``pip install jsonschema`` and write its own ``validate()``;
we don't paint into a corner.

Supported keywords:
  - ``type``: ``"object" | "array" | "string" | "integer" | "number" |
              "boolean" | "null"`` (or a list for union types)
  - ``properties``: object → property-name → sub-schema
  - ``required``: list of required property names
  - ``additionalProperties``: bool (default True). When False, extra keys
    cause a validation error.
  - ``items``: array → sub-schema applied to each element
  - ``enum``: list of allowed scalar values
  - ``nullable``: bool — convenience, equivalent to ``type: [..., "null"]``

Failures raise ``BrowserwrightError`` with a path-qualified message so the
agent can tell the user *which* field failed.
"""
from __future__ import annotations

from typing import Any

from .errors import BrowserwrightError


_TYPE_MAP = {
    "object": dict,
    "array": list,
    "string": str,
    "integer": int,
    "number": (int, float),
    "boolean": bool,
    "null": type(None),
}


class OutputSchemaError(BrowserwrightError):
    exit_code = 3

    def __init__(self, site: str, task: str, path: str, msg: str):
        self.site, self.task, self.path, self.msg_short = site, task, path, msg
        super().__init__(f"OUTPUT_SCHEMA mismatch in {site}/{task} at {path}: {msg}")


def validate(value: Any, schema: dict, *, site: str = "", task: str = "") -> None:
    """Raise ``OutputSchemaError`` on shape mismatch. Returns None on success."""
    _check(value, schema, path="$", site=site, task=task)


def _types_for(schema: dict) -> list:
    t = schema.get("type")
    if isinstance(t, list):
        out = [_TYPE_MAP[k] for k in t if k in _TYPE_MAP]
    elif isinstance(t, str):
        out = [_TYPE_MAP[t]] if t in _TYPE_MAP else []
    else:
        out = []
    if schema.get("nullable"):
        out.append(type(None))
    return out


def _check(value, schema, *, path, site, task):
    if not isinstance(schema, dict):
        return
    types = _types_for(schema)
    if types:
        # bool is a subclass of int in Python; treat them as distinct.
        if int in types and bool not in types and isinstance(value, bool):
            raise OutputSchemaError(site, task, path, f"expected {types}, got bool")
        if not isinstance(value, tuple(types)):
            raise OutputSchemaError(
                site, task, path,
                f"expected one of {[t.__name__ if isinstance(t, type) else t for t in types]}, "
                f"got {type(value).__name__}",
            )
    if "enum" in schema and value not in schema["enum"]:
        raise OutputSchemaError(site, task, path,
                                f"value {value!r} not in enum {schema['enum']!r}")
    if isinstance(value, dict) and "properties" in schema:
        props = schema.get("properties", {})
        for key in schema.get("required", []):
            if key not in value:
                raise OutputSchemaError(site, task, f"{path}.{key}", "missing required key")
        for k, v in value.items():
            sub = props.get(k)
            if sub is not None:
                _check(v, sub, path=f"{path}.{k}", site=site, task=task)
            elif schema.get("additionalProperties") is False:
                raise OutputSchemaError(site, task, f"{path}.{k}", "unexpected key")
    if isinstance(value, list) and "items" in schema:
        items_schema = schema["items"]
        for i, item in enumerate(value):
            _check(item, items_schema, path=f"{path}[{i}]", site=site, task=task)
