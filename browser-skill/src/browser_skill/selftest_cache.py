"""v0.2 selftest cache (spec §10 v0.2).

The runner pays a navigation cost on every task call because selftest() does
its own ``goto_url`` + assert. For tasks that get called repeatedly within a
short window this is wasteful — the site shape rarely drifts that fast.

The cache keys an "ok" verdict on:
  - site / task_name
  - the task file's content hash (any code edit invalidates)
  - the LAST_VERIFIED string (let humans force a re-run by bumping it)

A passing entry is good for ``CACHE_TTL_SEC`` (default 24h). After that, or on
any miss, the runner falls through to call ``selftest()`` itself; on success
the entry is refreshed.

Cache file: ``$BS_HOME/selftest_cache.json``. Single JSON object so we can
write atomically (write to .tmp + rename) and never end up with a corrupt
half-line.
"""
from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from pathlib import Path
from typing import Optional


CACHE_TTL_SEC = 24 * 3600


def _home() -> Path:
    return Path(os.path.expanduser(os.environ.get("BS_HOME", "~/.browser-skill")))


def _cache_path() -> Path:
    return _home() / "selftest_cache.json"


_lock = threading.Lock()


def _load_cache() -> dict:
    p = _cache_path()
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def _save_cache(data: dict) -> None:
    p = _cache_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(p)


def _task_hash(task_path: Path) -> str:
    try:
        return hashlib.sha256(task_path.read_bytes()).hexdigest()[:16]
    except OSError:
        return ""


def _key(site: str, name: str) -> str:
    return f"{site}/{name}"


def is_fresh(site: str, name: str, task_path: Path,
             ttl_sec: int = CACHE_TTL_SEC) -> bool:
    """Return True if the cache holds a recent pass for this (site, task).

    Bypass with env ``BS_SELFTEST_NOCACHE=1``.
    """
    if os.environ.get("BS_SELFTEST_NOCACHE"):
        return False
    with _lock:
        cache = _load_cache()
        entry = cache.get(_key(site, name))
    if not entry:
        return False
    if entry.get("verdict") != "ok":
        return False
    age = time.time() - float(entry.get("ts", 0))
    if age > ttl_sec:
        return False
    if entry.get("file_hash") != _task_hash(task_path):
        return False
    return True


def remember_pass(site: str, name: str, task_path: Path) -> None:
    """Record a successful selftest for this (site, task)."""
    with _lock:
        cache = _load_cache()
        cache[_key(site, name)] = {
            "verdict": "ok",
            "ts": time.time(),
            "file_hash": _task_hash(task_path),
        }
        _save_cache(cache)


def remember_fail(site: str, name: str, task_path: Path, reason: str) -> None:
    """Record a failure so the next list-tasks can flag it (UI / debugging).
    We do NOT use failure entries to skip selftest — failures must always
    re-run on the next attempt so transient flakes recover."""
    with _lock:
        cache = _load_cache()
        cache[_key(site, name)] = {
            "verdict": "fail",
            "ts": time.time(),
            "file_hash": _task_hash(task_path),
            "reason": reason[:500],
        }
        _save_cache(cache)


def invalidate(site: Optional[str] = None, name: Optional[str] = None) -> None:
    with _lock:
        cache = _load_cache()
        if site is None:
            cache.clear()
        elif name is None:
            cache = {k: v for k, v in cache.items() if not k.startswith(f"{site}/")}
        else:
            cache.pop(_key(site, name), None)
        _save_cache(cache)
