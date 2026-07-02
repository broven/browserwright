"""Backend Protocol + result types.

Spec §4.2: every backend answers exactly one question — "give me a browser-level
CDP ws URL or explain why you can't." Both `probe` (cheap, no side effects, drives
doctor) and `resolve` (returns a URL, still no ws open — only HTTP discovery /
filesystem reads) are mandatory.

The hard rule from §2.3 (handshake ↔ banner sync): even `resolve` must NOT open
a ws. Opening the ws is always the caller's job. Backends only return URLs.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, Protocol


# ---- enums ------------------------------------------------------------------

BackendKind = Literal["UPSTREAM_WS", "LOCAL_RELAY"]
Mode = Literal["A", "B"]
# §5.2 enum (locked under schema_version=1).
# v0.5 added "auth-required" for the cloud backend (header/mTLS auth must be
# configured before connect). Adding values to this enum is a minor schema
# bump — Skill code matches by value, so new entries don't break old skills,
# they just appear as "unknown ux_cost" until skill catches up.
UxCost = Literal[
    "none", "banner", "popup-per-ws+banner",
    "extension-permission", "auth-required",
]


# ---- result dataclasses -----------------------------------------------------

@dataclass
class ResolveResult:
    """Successful `resolve` return value.

    `extras` mirrors §5.1's `--json` extras block. `isolated_profile` is only
    meaningful for rdp; other backends leave it None.
    """
    ws_url: str
    backend: str
    extras: dict = field(default_factory=dict)


@dataclass
class DoctorResult:
    """Per-backend doctor entry. Field set is locked under schema_version=1
    (§5.2). Even when a value is unknown, the key must appear (=None) so Skill
    code never needs existence checks.

    `extras` (v0.5): per-backend free-form dict for backend-specific fields
    that don't fit the locked schema. Skill code uses these via
    `entry["extras"].get("...")`. Adding extras keys is a per-backend minor
    bump, not a schema-level bump — `schema_version` stays 1.
    """
    name: str
    available: bool
    ws_url: str | None = None          # always None unless --probe-ws fired and succeeded
    detail: str = ""
    ux_warning: str | None = None
    needs_user_action: str | None = None
    ux_cost: UxCost = "none"
    extras: dict = field(default_factory=dict)


# ---- Protocol ---------------------------------------------------------------

class Backend(Protocol):
    """All four v0.1 backends implement this. The Protocol is structural; no
    ABC inheritance required — a duck-typed class works (mirrors browser-harness
    style: protocols, not registries).
    """

    name: str
    kind: BackendKind
    recommended_mode: Mode
    ux_cost: UxCost

    async def probe(self) -> DoctorResult:
        """Cheap, side-effect-free. NO ws open. NO Chrome popup."""
        ...

    async def resolve(self, timeout: float) -> ResolveResult:
        """Return a ws URL, or raise `Unavailable`. Still no ws open."""
        ...
