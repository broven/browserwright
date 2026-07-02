"""site-skills index + simple ranking (spec §E)."""
from __future__ import annotations

import datetime as _dt
import importlib.util
import json
import re
from pathlib import Path
from typing import Any, Optional

from .memory import _md
from .memory.site_mem import site_skills_root, site_skills_roots


def _bundled_root() -> Path:
    return Path(__file__).resolve().parent / "site_skills_starter"


def _iter_site_dirs() -> list[Path]:
    """Project → ``$BS_HOME`` → bundled, dedup by site name.

    The order encodes the precedence promise: project workspace is always
    king, then per-user, then the bundled starter set as fallback.
    """
    roots = [*site_skills_roots(), _bundled_root()]
    seen: set[str] = set()
    out: list[Path] = []
    for root in roots:
        if not root.exists():
            continue
        for child in sorted(root.iterdir()):
            if not child.is_dir():
                continue
            if child.name in seen:
                continue
            if not (child / "tasks").exists() and not (child / "memory.md").exists():
                continue
            seen.add(child.name)
            out.append(child)
    return out


def _load_task_meta(task_py: Path) -> dict[str, Any]:
    spec = importlib.util.spec_from_file_location(task_py.stem, task_py)
    if not spec or not spec.loader:
        return {"name": task_py.stem}
    mod = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(mod)
    except Exception:
        return {"name": task_py.stem, "load_error": True}
    return {
        "name": task_py.stem,
        "desc": (getattr(mod, "__doc__", "") or "").strip().splitlines()[:1][0]
                if getattr(mod, "__doc__", "") else "",
        "args": getattr(mod, "ARGS", {}),
        "output": getattr(mod, "OUTPUT", "Any"),
        "tags": getattr(mod, "TAGS", []),
        "requires_login": bool(getattr(mod, "REQUIRES_LOGIN", False)),
        "last_verified": getattr(mod, "LAST_VERIFIED", None),
        "broken_since": getattr(mod, "BROKEN_SINCE", None),
        "path": str(task_py.resolve()),
    }


def _load_site_entry(site_dir: Path) -> dict[str, Any]:
    fm = {}
    if (site_dir / "memory.md").exists():
        fm, _body = _md.parse_doc((site_dir / "memory.md").read_text(encoding="utf-8"))
    desc = ""
    if (site_dir / "SKILL.md").exists():
        first = ((site_dir / "SKILL.md").read_text(encoding="utf-8")).strip().splitlines()
        if first:
            desc = first[0].lstrip("# ").strip()
    tasks_dir = site_dir / "tasks"
    tasks = []
    if tasks_dir.exists():
        for t in sorted(tasks_dir.glob("*.py")):
            tasks.append(_load_task_meta(t))
    return {
        "site": site_dir.name,
        "host_patterns": fm.get("host_patterns", []),
        "aliases": fm.get("aliases", []),
        "description_first_line": desc,
        "tasks": tasks,
        "path": str(site_dir.resolve()),
    }


def rebuild_index() -> dict[str, Any]:
    out = {
        "version": 1,
        "generated_at": _dt.datetime.now(_dt.timezone.utc).isoformat(),
        "sites": [_load_site_entry(d) for d in _iter_site_dirs()],
    }
    target = site_skills_root() / "index.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(out, indent=2, default=str), encoding="utf-8")
    return out


def _load_index() -> dict[str, Any]:
    target = site_skills_root() / "index.json"
    if not target.exists():
        return rebuild_index()
    try:
        return json.loads(target.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return rebuild_index()


# ---- ranking ----------------------------------------------------------


def _tokens(s: str) -> set[str]:
    return {p for p in re.split(r"[\s/_\-]+", (s or "").lower()) if p}


def _jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def _days_since(date_str: Optional[str]) -> int:
    if not date_str:
        return 9999
    try:
        d = _dt.date.fromisoformat(date_str[:10])
    except ValueError:
        return 9999
    return (_dt.date.today() - d).days


def score(query: str, site_entry: dict, task: dict) -> float:
    q = (query or "").lower()
    s = 0.0
    for alias in site_entry.get("aliases", []):
        if alias and alias.lower() in q:
            s += 1.0
            break
    for h in site_entry.get("host_patterns", []):
        if h and h.split(".")[0] in q:
            s += 0.5
            break
    s += 0.3 * _jaccard(_tokens(task.get("desc", "")), _tokens(query or ""))
    for t in task.get("tags", []):
        if t and t.lower() in q:
            s += 0.2
    if task.get("broken_since"):
        s -= 0.5
    if _days_since(task.get("last_verified")) < 30:
        s += 0.1
    return s


def list_tasks(*, site: Optional[str] = None, query: Optional[str] = None,
               limit: int = 20) -> list[dict[str, Any]]:
    index = _load_index()
    out: list[dict[str, Any]] = []
    for entry in index.get("sites", []):
        if site and entry["site"] != site:
            continue
        for t in entry.get("tasks", []):
            row = {
                "site": entry["site"],
                "name": t["name"],
                "desc": t.get("desc", ""),
                "args": t.get("args", {}),
                "output": t.get("output", "Any"),
                "tags": t.get("tags", []),
                "requires_login": t.get("requires_login", False),
                "last_verified": t.get("last_verified"),
                "broken_since": t.get("broken_since"),
                "path": t.get("path"),
            }
            if query:
                row["match_score"] = round(score(query, entry, t), 3)
            out.append(row)
    if query:
        out.sort(key=lambda r: r.get("match_score", 0), reverse=True)
    return out[:limit]


def find_task_path(site: str, name: str) -> Path:
    """Return absolute path to ``site-skills/<site>/tasks/<name>.py``,
    consulting project → $BS_HOME → bundled in that order.
    Raises ``FileNotFoundError``.

    Site-name normalisation (Bug 1, v0.3.1): if the literal ``site`` arg
    doesn't resolve, retry with ``host_stem(site)`` so the caller can pass
    a raw URL or hostname (e.g. ``news.ycombinator.com`` from a CLI
    invocation) and still hit the eTLD+1-named bundled directory
    (``ycombinator.com``).
    """
    from .memory.site_mem import host_stem
    roots = (*site_skills_roots(), _bundled_root())
    candidates: list[str] = [site]
    stem = host_stem(site)
    if stem and stem != site:
        candidates.append(stem)
    for s in candidates:
        for root in roots:
            cand = root / s / "tasks" / f"{name}.py"
            if cand.exists():
                return cand
    raise FileNotFoundError(f"{site}/{name}")
