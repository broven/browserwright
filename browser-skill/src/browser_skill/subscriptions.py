"""Subscription scaffolding (v0.3).

Lets a user / agent pull third-party ``site-skills`` collections from git
repositories and have them automatically picked up by discovery::

    browser-skill sub add https://github.com/someone/example-skills
    browser-skill sub list
    browser-skill sub update              # git pull every subscription
    browser-skill sub update --name foo   # only one
    browser-skill sub remove --name foo

Subscriptions land in ``$BS_HOME/subscriptions/<name>/`` (the ``<name>``
defaults to the git repo's basename). Discovery layers them between project-
local ``./site-skills/`` and ``$BS_HOME/site-skills/`` — higher priority than
the per-user pool because they're an explicit dependency the user committed
to, lower than the project because the project workspace always wins.

A subscription's ``site-skills/`` subdir (or root if the repo lays sites
directly under root) is what discovery iterates. We also write a
``$BS_HOME/subscriptions/.metadata.json`` index so ``sub list`` / ``sub
update`` know what's installed without re-walking the FS each time.

Implementation note: we shell out to ``git`` (no GitPython dep). If git is
missing on the system, ``sub add`` returns a clear error.
"""
from __future__ import annotations

import datetime as _dt
import json
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Iterable, Optional

from .memory.global_mem import home_dir


_NAME_RX = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")


def subscriptions_root() -> Path:
    return home_dir() / "subscriptions"


def metadata_path() -> Path:
    return subscriptions_root() / ".metadata.json"


def _load_metadata() -> dict:
    p = metadata_path()
    if not p.exists():
        return {"version": 1, "subs": {}}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {"version": 1, "subs": {}}


def _save_metadata(data: dict) -> None:
    p = metadata_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(p)


def _git_available() -> bool:
    return shutil.which("git") is not None


def _derive_name(url: str) -> str:
    """Default ``--name`` value from the repo URL — last path component minus ``.git``."""
    leaf = url.rstrip("/").rsplit("/", 1)[-1]
    if leaf.endswith(".git"):
        leaf = leaf[:-4]
    leaf = re.sub(r"[^A-Za-z0-9_.-]+", "-", leaf).strip("-_.")
    return leaf or "sub"


# ---- public CLI surface ----------------------------------------------


class SubscriptionError(Exception):
    pass


def add(url: str, *, name: Optional[str] = None) -> dict:
    """Clone ``url`` into ``$BS_HOME/subscriptions/<name>/``. Idempotent on
    name — if the dir already exists with the same URL it's a no-op."""
    if not _git_available():
        raise SubscriptionError(
            "git not found on PATH — install git or copy the site-skills "
            "directory manually into $BS_HOME/subscriptions/"
        )
    name = name or _derive_name(url)
    if not _NAME_RX.match(name):
        raise SubscriptionError(
            f"invalid subscription name {name!r} — use [A-Za-z0-9_.-] only"
        )
    target = subscriptions_root() / name
    if target.exists():
        meta = _load_metadata()
        existing = meta["subs"].get(name, {})
        if existing.get("url") == url:
            return {"name": name, "url": url, "path": str(target), "status": "already_present"}
        raise SubscriptionError(
            f"{target} already exists with a different URL "
            f"({existing.get('url')!r}); remove it first"
        )
    target.parent.mkdir(parents=True, exist_ok=True)
    proc = subprocess.run(
        ["git", "clone", "--depth", "1", url, str(target)],
        capture_output=True, text=True, timeout=120,
    )
    if proc.returncode != 0:
        # Clean up partial clone before reporting.
        if target.exists():
            shutil.rmtree(target, ignore_errors=True)
        raise SubscriptionError(
            f"git clone failed (exit {proc.returncode}): {(proc.stderr or proc.stdout).strip()}"
        )
    meta = _load_metadata()
    meta["subs"][name] = {
        "url": url,
        "added_at": _dt.datetime.now(_dt.timezone.utc).replace(microsecond=0).isoformat(),
        "last_updated": None,
    }
    _save_metadata(meta)
    return {"name": name, "url": url, "path": str(target), "status": "added"}


def remove(name: str) -> dict:
    """Delete ``$BS_HOME/subscriptions/<name>/`` and drop its metadata entry."""
    target = subscriptions_root() / name
    if not target.exists():
        raise SubscriptionError(f"no subscription named {name!r}")
    shutil.rmtree(target)
    meta = _load_metadata()
    meta["subs"].pop(name, None)
    _save_metadata(meta)
    return {"name": name, "status": "removed"}


def update(names: Optional[Iterable[str]] = None) -> list[dict]:
    """``git pull`` each subscription. ``names=None`` updates all."""
    if not _git_available():
        raise SubscriptionError("git not found on PATH")
    meta = _load_metadata()
    targets = list(names) if names else list(meta["subs"].keys())
    out: list[dict] = []
    for n in targets:
        path = subscriptions_root() / n
        if not path.exists():
            out.append({"name": n, "status": "missing"})
            continue
        proc = subprocess.run(
            ["git", "-C", str(path), "pull", "--ff-only"],
            capture_output=True, text=True, timeout=120,
        )
        if proc.returncode != 0:
            out.append({"name": n, "status": "error",
                        "detail": (proc.stderr or proc.stdout).strip()})
            continue
        meta["subs"].setdefault(n, {})["last_updated"] = (
            _dt.datetime.now(_dt.timezone.utc).replace(microsecond=0).isoformat()
        )
        out.append({"name": n, "status": "updated",
                    "detail": proc.stdout.strip().splitlines()[-1] if proc.stdout else ""})
    _save_metadata(meta)
    return out


def list_all() -> list[dict]:
    meta = _load_metadata()
    out: list[dict] = []
    for name, info in sorted(meta["subs"].items()):
        path = subscriptions_root() / name
        out.append({
            "name": name,
            "url": info.get("url"),
            "added_at": info.get("added_at"),
            "last_updated": info.get("last_updated"),
            "path": str(path),
            "exists": path.exists(),
        })
    return out


# ---- discovery hook --------------------------------------------------


def iter_subscription_site_roots() -> list[Path]:
    """Return every subscription's site-skills root, in stable name order.

    A subscription may either lay sites directly at its repo root (treat the
    repo root as the site-skills root) or under a ``site-skills/`` subdir.
    We detect the latter automatically.
    """
    root = subscriptions_root()
    if not root.is_dir():
        return []
    out: list[Path] = []
    for child in sorted(root.iterdir()):
        if not child.is_dir() or child.name.startswith("."):
            continue
        nested = child / "site-skills"
        if nested.is_dir():
            out.append(nested)
        else:
            out.append(child)
    return out
