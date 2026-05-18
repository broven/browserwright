"""Site-level memory — ``site-skills/<host-stem>/memory.md``."""
from __future__ import annotations

import datetime as _dt
import fcntl
import os
import re
import threading
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

from . import _md
from .global_mem import home_dir


# ---- redaction --------------------------------------------------------

_HIGH_ENTROPY_RE = re.compile(r"[A-Za-z0-9_\-]{32,}")
_BEARER_RE = re.compile(r"\bBearer\s+[A-Za-z0-9._\-]+", re.IGNORECASE)
_USER_PATH_RE = re.compile(r"/Users/[A-Za-z0-9_.\-]+/")
_HOME_PATH_RE = re.compile(r"/home/[A-Za-z0-9_.\-]+/")
_COOKIE_RE = re.compile(r"\b(?:set-cookie|cookie|session_id|csrf[_-]token)\s*[:=]", re.IGNORECASE)
_CARD_RE = re.compile(r"\b(?:\d[ -]?){13,19}\b")


REDACTION_REASONS = {
    "high_entropy": _HIGH_ENTROPY_RE,
    "bearer_token": _BEARER_RE,
    "user_path": _USER_PATH_RE,
    "home_path": _HOME_PATH_RE,
    "cookie_or_session": _COOKIE_RE,
    "card_number": _CARD_RE,
}


def redact_check(text: str) -> list[str]:
    """Return a list of reason codes that ``text`` triggered.

    Empty list = safe to write. We're conservative (reject on hit) per
    spec §C.3 rule 2.
    """
    hits: list[str] = []
    for reason, rx in REDACTION_REASONS.items():
        if rx.search(text):
            hits.append(reason)
    return hits


# ---- host → directory stem -------------------------------------------


_STEM_OVERRIDES = {
    # spec §B.1: short aliases that beat the algorithmic eTLD+1 choice for a
    # handful of high-value sites with non-obvious natural names.
    "zhipin.com": "boss-zhipin",
    "boss.zhipin.com": "boss-zhipin",
    "www.zhipin.com": "boss-zhipin",
    "mail.google.com": "gmail",
}


# Minimum viable multi-label TLD set so eTLD+1 picks the right "registered
# name + TLD" for cc-suffix hosts like ``bbc.co.uk``. We don't ship a full
# Public Suffix List — just the buckets we'll actually hit. Add more if a
# site is being mis-stemmed in practice.
_MULTI_LABEL_TLDS = {
    "co.uk", "ac.uk", "gov.uk", "org.uk", "net.uk", "me.uk",
    "co.jp", "ac.jp", "or.jp", "ne.jp", "go.jp",
    "co.kr", "or.kr", "ne.kr",
    "com.cn", "net.cn", "org.cn", "gov.cn", "edu.cn",
    "com.hk", "com.tw", "com.sg", "com.au", "net.au", "org.au",
    "com.br", "com.mx", "com.ar",
    "co.in", "co.nz", "co.za",
}


def _split_host(host_or_url: str) -> str:
    if "://" in host_or_url:
        host = urlparse(host_or_url).hostname or host_or_url
    else:
        host = host_or_url
    # FQDN form sometimes carries a trailing dot (``github.com.``);
    # treat it as equivalent to the trimmed form. REVIEW.md F-9.
    return (host or "").lower().strip().strip(".")


def host_stem(host_or_url: str) -> str:
    """Return the on-disk site-dir name for a host or URL.

    Algorithm (changed in v0.3.1 — Bug 1 in AI E2E run):
      1. ``_STEM_OVERRIDES`` matches the literal lowercased hostname.
      2. Otherwise return **eTLD+1** — the registered name plus its TLD.
         For two-label TLDs (``co.uk``, ``com.cn`` …) keep three labels.
         Examples: ``news.ycombinator.com → ycombinator.com``,
         ``en.wikipedia.org → wikipedia.org``,
         ``www.google.com → google.com``,
         ``shop.example.co.uk → example.co.uk``.

    The pre-v0.3.1 algorithm returned only the first label
    (``news.ycombinator.com → news``); on-disk memory written under those
    short stems is still readable via ``_read_candidates()`` fallback in
    ``SiteMemory.read()``.
    """
    host = _split_host(host_or_url)
    if host in _STEM_OVERRIDES:
        return _STEM_OVERRIDES[host]
    parts = host.split(".") if host else []
    if len(parts) < 2:
        return host or "unknown"
    last_two = ".".join(parts[-2:])
    if last_two in _MULTI_LABEL_TLDS and len(parts) >= 3:
        return ".".join(parts[-3:])
    return last_two


def _legacy_host_stem(host_or_url: str) -> str:
    """Pre-v0.3.1 short-label stem. Kept for *read* fallback only — any
    user-written memory landed under this stem before Bug 1 was patched
    (e.g. ``site-skills/news/memory.md`` for ``news.ycombinator.com``).
    Never used for new writes.
    """
    host = _split_host(host_or_url)
    if host in _STEM_OVERRIDES:
        return _STEM_OVERRIDES[host]
    parts = host.split(".") if host else []
    if len(parts) >= 2 and parts[0] in ("www", "m"):
        parts = parts[1:]
    if len(parts) >= 2:
        return parts[0]
    return host or "unknown"


# ---- locations -------------------------------------------------------


def site_skills_root() -> Path:
    """Where ``bootstrap_site`` / ``remember`` *write* a new site.

    Precedence (v0.2): project-local ``./site-skills/`` if it exists →
    ``$BS_HOME/site-skills/`` otherwise. Reads use ``site_skills_roots()``
    which layers project on top of home on top of the bundled starter.
    """
    cwd = Path.cwd() / "site-skills"
    if cwd.is_dir():
        return cwd
    return home_dir() / "site-skills"


def site_skills_roots() -> list[Path]:
    """All roots consulted for *reads*, in precedence order
    (highest priority first):

      1. ``./site-skills/`` — project-local, git-tracked.
      2. ``$BS_HOME/site-skills/`` — user-global, agent-written.
      3. bundled starter directory shipped with the package.

    The first hit per site name wins. Discovery enforces this; writes always
    target the writable ``site_skills_root()``.
    """
    roots: list[Path] = []
    cwd = Path.cwd() / "site-skills"
    if cwd.is_dir():
        roots.append(cwd)
    hr = home_dir() / "site-skills"
    if hr.is_dir():
        roots.append(hr)
    return roots


def site_dir(host: str) -> Path:
    return site_skills_root() / host_stem(host)


def memory_path(host: str) -> Path:
    return site_dir(host) / "memory.md"


# ---- file ops --------------------------------------------------------


class _FileLock:
    def __init__(self, path: Path):
        self.path = path
        self._fd: Optional[int] = None
        self._t = threading.Lock()

    def __enter__(self):
        self._t.acquire()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(self._fd, fcntl.LOCK_EX)
        except OSError:
            pass
        return self

    def __exit__(self, *exc):
        try:
            if self._fd is not None:
                try:
                    fcntl.flock(self._fd, fcntl.LOCK_UN)
                except OSError:
                    pass
                os.close(self._fd)
        finally:
            self._fd = None
            self._t.release()


_BOOT_BODY = """# {stem} site memory

This file is append-only (mostly). Each section is meant for a specific kind
of fact — see the headings below.

## Notes

## Known traps

## 顶层 URL 结构

## 私有 API

## 用户偏好

## Task history
"""


def bootstrap_site(host: str, aliases: Optional[list[str]] = None) -> Path:
    """Lazy-create ``site-skills/<stem>/`` with the canonical layout. Returns
    the directory path. Idempotent: noop if already present.

    spec §A.2 / §C.3: called automatically by ``remember(host, ...)`` if the
    directory doesn't exist yet (US2 in-flight write).
    """
    stem = host_stem(host)
    d = site_dir(host)
    if d.exists() and (d / "memory.md").exists():
        return d
    d.mkdir(parents=True, exist_ok=True)
    mem = d / "memory.md"
    if not mem.exists():
        fm = {
            "site": stem,
            "host_patterns": _candidate_patterns(host),
            "aliases": list(aliases or []),
            "last_updated": _dt.date.today().isoformat(),
        }
        body = _BOOT_BODY.format(stem=stem)
        _md.write_atomic(mem, _md.render_doc(fm, body))
    skill = d / "SKILL.md"
    if not skill.exists():
        skill.write_text(
            f"# {stem}\n\nStub site skill. Add a section per task here as they get solidified.\n",
            encoding="utf-8",
        )
    (d / "tasks").mkdir(exist_ok=True)
    return d


def _candidate_patterns(host: str) -> list[str]:
    if "://" in host:
        host = urlparse(host).hostname or host
    host = host.lower()
    parts = host.split(".")
    pats = {host}
    if len(parts) >= 2 and parts[0] in ("www", "m"):
        pats.add(".".join(parts[1:]))
    elif len(parts) >= 2:
        pats.add("www." + host)
    return sorted(pats)


# ---- SiteMemory class ------------------------------------------------


class _RedactionRejected(Exception):
    """remember() refused to write because the text triggered redaction."""

    def __init__(self, reasons: list[str], text: str):
        self.reasons, self.text = reasons, text
        super().__init__(f"refused to write site memory (reasons: {reasons})")


class SiteMemory:
    def __init__(self, host: str):
        self.host = host
        self.stem = host_stem(host)
        self.dir = site_dir(host)
        # Writable target is always the new-style eTLD+1 path. Reads may
        # transparently fall back to the legacy short-stem path via
        # ``_read_candidates`` (Bug 1 back-compat).
        self.path = memory_path(host)

    def _read_candidates(self) -> list[Path]:
        """Memory.md paths to try, in order. New-style first; legacy short
        stem second when distinct. Lets v0.3.1+ keep reading data written
        under the pre-fix short stem (e.g. ``site-skills/news/memory.md``
        for ``news.ycombinator.com``)."""
        seen: set[Path] = set()
        out: list[Path] = []
        for p in (self.path,):
            if p not in seen:
                out.append(p)
                seen.add(p)
        legacy_stem = _legacy_host_stem(self.host)
        if legacy_stem != self.stem:
            for root in (Path.cwd() / "site-skills",
                         home_dir() / "site-skills"):
                p = root / legacy_stem / "memory.md"
                if p not in seen:
                    out.append(p)
                    seen.add(p)
        return out

    def ensure(self) -> None:
        if not self.path.exists():
            bootstrap_site(self.host)

    def append(self, text: str, *, section: str = "Notes") -> Path:
        text = text.strip()
        if not text:
            return self.path
        hits = redact_check(text)
        if hits:
            raise _RedactionRejected(hits, text)
        self.ensure()
        with _FileLock(self.path):
            fm, body = _md.parse_doc(self.path.read_text(encoding="utf-8"))
            fm = fm or {}
            fm["last_updated"] = _dt.date.today().isoformat()
            new_body = _md.append_to_section(body, section, f"- {text}")
            _md.write_atomic(self.path, _md.render_doc(fm, new_body))
        return self.path

    def read(self) -> dict:
        for p in self._read_candidates():
            if p.exists():
                fm, body = _md.parse_doc(p.read_text(encoding="utf-8"))
                return {"frontmatter": fm, "body": body}
        return {"frontmatter": {}, "body": ""}

    def find(self, pattern: str) -> list[tuple[int, str]]:
        """Return bullet lines that match ``pattern``. Used by ``forget``."""
        if not self.path.exists():
            return []
        _fm, body = _md.parse_doc(self.path.read_text(encoding="utf-8"))
        return _md.find_matching_lines(body, pattern)

    def forget(self, pattern: str, *, confirm: bool = True) -> list[str]:
        """Remove every bullet whose text contains ``pattern``. Returns the
        removed lines (for audit). v0.2 — see spec §10.

        ``confirm=True`` makes the first call a dry-run that just returns the
        matches; pass ``confirm=False`` after the user assents to perform the
        actual delete. This mirrors ``remember_preference`` (US4) — destructive
        ops always ask first.
        """
        if not self.path.exists():
            return []
        matches = self.find(pattern)
        if not matches:
            return []
        if confirm:
            return [ln for _i, ln in matches]
        with _FileLock(self.path):
            fm, body = _md.parse_doc(self.path.read_text(encoding="utf-8"))
            fm = fm or {}
            fm["last_updated"] = _dt.date.today().isoformat()
            new_body = _md.remove_lines(body, {i for i, _ln in matches})
            _md.write_atomic(self.path, _md.render_doc(fm, new_body))
        return [ln for _i, ln in matches]


def site_memory(host: str) -> SiteMemory:
    return SiteMemory(host)


# Re-export for callers that catch the redaction error specifically.
RedactionRejected = _RedactionRejected
