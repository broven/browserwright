"""Parse Tampermonkey-style ``==UserScript==`` headers into structured records.

v1 capability surface is plain page JS (no GM_* APIs); unsupported metadata
directives are collected as warnings rather than rejected, so existing
userscripts paste in without crashing.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field

DEFAULT_NAMESPACE = "bd.userscripts"
_RUN_AT = {
    "document-start": "document_start",
    "document-end": "document_end",
    "document-idle": "document_idle",
    "document_start": "document_start",
    "document_end": "document_end",
    "document_idle": "document_idle",
}
_SUPPORTED = {
    "name",
    "namespace",
    "match",
    "include",
    "exclude",
    "run-at",
    "version",
    "description",
}
_HEADER_RE = re.compile(
    r"//\s*==UserScript==\s*\n(.*?)//\s*==/UserScript==", re.DOTALL)
_LINE_RE = re.compile(r"//\s*@(\S+)\s+(.*?)\s*$")

# Chrome match-pattern grammar: ``<scheme>://<host><path>`` (or ``<all_urls>``).
# Validating here lets the daemon reject typos loudly instead of shipping a
# pattern that makes ``chrome.userScripts.register`` reject the whole batch on
# the extension side (which would silently disable every resident script).
_MATCH_PATTERN_RE = re.compile(
    r"^(\*|https?|file|ftp|wss?)://(\*|(\*\.)?[^/*]+)?(/.*)$"
)


def _is_valid_match_pattern(pattern: str) -> bool:
    return pattern == "<all_urls>" or bool(_MATCH_PATTERN_RE.match(pattern))


class UserscriptParseError(ValueError):
    """Raised when a userscript has no header or no match pattern."""


@dataclass
class Userscript:
    name: str
    namespace: str
    matches: list[str]
    exclude_matches: list[str]
    run_at: str
    version: str
    description: str
    code: str
    warnings: list[str] = field(default_factory=list)

    @property
    def identity(self) -> str:
        return f"{self.namespace}/{self.name}"

    @property
    def id(self) -> str:
        return hashlib.sha1(self.identity.encode()).hexdigest()[:12]

    def to_payload(self) -> dict:
        return {
            "id": self.id,
            "identity": self.identity,
            "name": self.name,
            "namespace": self.namespace,
            "matches": self.matches,
            "excludeMatches": self.exclude_matches,
            "runAt": self.run_at,
            "version": self.version,
            "description": self.description,
            "code": self.code,
            "warnings": self.warnings,
        }


def parse_userscript(text: str) -> Userscript:
    match = _HEADER_RE.search(text)
    if not match:
        raise UserscriptParseError("missing ==UserScript== metadata block")
    block = match.group(1)
    code = text[match.end():].lstrip("\n")

    name = ""
    namespace = ""
    version = ""
    description = ""
    run_at = "document_idle"
    matches: list[str] = []
    excludes: list[str] = []
    warnings: list[str] = []

    for line in block.splitlines():
        line_match = _LINE_RE.match(line.strip())
        if not line_match:
            continue
        key, value = line_match.group(1).lower(), line_match.group(2).strip()
        if key == "name":
            name = value
        elif key == "namespace":
            namespace = value
        elif key in ("match", "include"):
            if _is_valid_match_pattern(value):
                matches.append(value)
            else:
                warnings.append(
                    f"@{key} {value!r} is not a valid match pattern (ignored)")
        elif key == "exclude":
            if _is_valid_match_pattern(value):
                excludes.append(value)
            else:
                warnings.append(
                    f"@exclude {value!r} is not a valid match pattern (ignored)")
        elif key == "run-at":
            run_at = _RUN_AT.get(value, "document_idle")
        elif key == "version":
            version = value
        elif key == "description":
            description = value
        elif key not in _SUPPORTED:
            warnings.append(f"@{key} not supported in v1 (ignored)")

    if not name:
        raise UserscriptParseError("@name is required")
    if not matches:
        raise UserscriptParseError("at least one @match/@include is required")

    return Userscript(
        name=name,
        namespace=namespace or DEFAULT_NAMESPACE,
        matches=matches,
        exclude_matches=excludes,
        run_at=run_at,
        version=version,
        description=description,
        code=code,
        warnings=warnings,
    )
