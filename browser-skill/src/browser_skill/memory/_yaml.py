"""Tiny YAML subset for memory frontmatter.

We don't pull in PyYAML — frontmatter blocks in spec §C.2 are simple:
top-level keys, scalar / list / nested-dict values one level deep, ISO dates
as strings. Hand-rolling a parser keeps install lean and the surface tiny.

If a frontmatter ever needs richer YAML (anchors, multi-line strings,
explicit typing) we can swap to PyYAML — but lazy-loading keeps the
zero-dep default.
"""
from __future__ import annotations

import datetime as _dt
from typing import Any


def _scalar(s: str) -> Any:
    s = s.strip()
    if not s:
        return ""
    if s in ("null", "~"):
        return None
    if s in ("true", "True"):
        return True
    if s in ("false", "False"):
        return False
    if (s.startswith("\"") and s.endswith("\"")) or (s.startswith("'") and s.endswith("'")):
        return s[1:-1]
    if s.startswith("[") and s.endswith("]"):
        body = s[1:-1].strip()
        if not body:
            return []
        return [_scalar(p.strip()) for p in _split_top_level(body, ",")]
    if s.startswith("{") and s.endswith("}"):
        body = s[1:-1].strip()
        if not body:
            return {}
        out: dict[str, Any] = {}
        for part in _split_top_level(body, ","):
            if ":" not in part:
                continue
            k, v = part.split(":", 1)
            out[_scalar(k.strip())] = _scalar(v.strip())
        return out
    # numbers
    try:
        if s.startswith("0") and "." not in s and len(s) > 1:
            raise ValueError
        return int(s)
    except ValueError:
        pass
    try:
        return float(s)
    except ValueError:
        pass
    return s


def _split_top_level(text: str, sep: str) -> list[str]:
    """Split ``text`` on ``sep`` while respecting (), [], {} nesting and quotes."""
    parts: list[str] = []
    buf: list[str] = []
    depth = 0
    quote: str | None = None
    for ch in text:
        if quote:
            buf.append(ch)
            if ch == quote:
                quote = None
            continue
        if ch in "\"'":
            quote = ch
            buf.append(ch)
            continue
        if ch in "[{(":
            depth += 1
        elif ch in "]})":
            depth -= 1
        if ch == sep and depth == 0:
            parts.append("".join(buf))
            buf = []
            continue
        buf.append(ch)
    if buf:
        parts.append("".join(buf))
    return parts


def loads(text: str) -> dict[str, Any]:
    """Parse a tiny-YAML frontmatter block. Supports::

        key: value
        key:
          nested: value
          list:
            - item
        list_inline: [a, b, c]
    """
    lines = text.splitlines()
    return _parse_block(lines, 0, indent=0)[0]


def _parse_block(lines: list[str], i: int, *, indent: int) -> tuple[dict[str, Any], int]:
    out: dict[str, Any] = {}
    while i < len(lines):
        raw = lines[i]
        if not raw.strip() or raw.lstrip().startswith("#"):
            i += 1
            continue
        cur_indent = len(raw) - len(raw.lstrip(" "))
        if cur_indent < indent:
            break
        if cur_indent > indent:
            i += 1
            continue
        line = raw.strip()
        if ":" not in line:
            i += 1
            continue
        key, rest = line.split(":", 1)
        key = key.strip()
        rest = rest.strip()
        if rest:
            out[key] = _scalar(rest)
            i += 1
            continue
        # Nested. Could be list (- starts) or map.
        nxt = i + 1
        # Skip blanks
        while nxt < len(lines) and not lines[nxt].strip():
            nxt += 1
        if nxt < len(lines):
            child = lines[nxt]
            child_indent = len(child) - len(child.lstrip(" "))
            stripped = child.lstrip(" ")
            if child_indent > indent and stripped.startswith("- "):
                # list
                items, i = _parse_list(lines, nxt, indent=child_indent)
                out[key] = items
                continue
            if child_indent > indent:
                nested, i = _parse_block(lines, nxt, indent=child_indent)
                out[key] = nested
                continue
        out[key] = None
        i += 1
    return out, i


def _parse_list(lines: list[str], i: int, *, indent: int) -> tuple[list[Any], int]:
    items: list[Any] = []
    while i < len(lines):
        raw = lines[i]
        if not raw.strip():
            i += 1
            continue
        cur_indent = len(raw) - len(raw.lstrip(" "))
        if cur_indent < indent:
            break
        stripped = raw.lstrip(" ")
        if not stripped.startswith("- "):
            break
        items.append(_scalar(stripped[2:].strip()))
        i += 1
    return items, i


# -- dump ---------------------------------------------------------------


def _dump_scalar(v: Any) -> str:
    if v is None:
        return "null"
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return repr(v)
    if isinstance(v, _dt.datetime):
        return v.isoformat()
    if isinstance(v, str):
        if v == "" or any(c in v for c in ":#{}[],&*?|<>=!%@`") or v.startswith(("-", "?", ":", "!")):
            return "\"" + v.replace("\\", "\\\\").replace("\"", "\\\"") + "\""
        return v
    raise TypeError(f"unsupported scalar type: {type(v).__name__}")


def dumps(data: dict[str, Any]) -> str:
    out: list[str] = []
    _emit(data, indent=0, out=out)
    return "\n".join(out) + ("\n" if out else "")


def _emit(value: Any, *, indent: int, out: list[str]) -> None:
    pad = " " * indent
    if isinstance(value, dict):
        for k, v in value.items():
            if isinstance(v, dict) and v:
                out.append(f"{pad}{k}:")
                _emit(v, indent=indent + 2, out=out)
            elif isinstance(v, list):
                if not v:
                    out.append(f"{pad}{k}: []")
                else:
                    out.append(f"{pad}{k}:")
                    for item in v:
                        if isinstance(item, (dict, list)):
                            out.append(f"{pad}  - ")
                            _emit(item, indent=indent + 4, out=out)
                        else:
                            out.append(f"{pad}  - {_dump_scalar(item)}")
            else:
                out.append(f"{pad}{k}: {_dump_scalar(v)}")
    elif isinstance(value, list):
        for item in value:
            out.append(f"{pad}- {_dump_scalar(item)}")
    else:
        out.append(f"{pad}{_dump_scalar(value)}")
