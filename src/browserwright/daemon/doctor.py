"""doctor + list-backends subcommands.

Spec §5.2: doctor probes every backend (or a specific one) and outputs a JSON
object. The shape is locked — every backend must appear, every key must be
present even when null, and adding a key in v0.x requires version bump.

Default behavior: ZERO ws side effects. `--probe-ws` is opt-in and explicitly
not implemented in v0.1 beyond a clear "not yet" message — the spec mentions
the flag but the v0.1 scope (§7) does not include real ws handshake.
"""
from __future__ import annotations

import asyncio

from .backends import all_backends, names
from .config import Config
from .errors import UserError
from .probe import daemon_status_async


# schema_version bumped to 2 in v0.5.3 (REVIEW.md F-1+F-2). v2 contract:
#   - `DoctorResult` gained `extras: dict` (free-form per-backend payload)
# v1 clients that strict-check `ux_cost in {none,banner,popup,extension-permission}`
# or that count backend-entry keys (==7) will break against v0.5+ daemons —
# they must be updated to v2-aware. Schema-lock test enforces no further
# silent drift; future field additions require another version bump.
#
# schema_version bumped to 3 for issue #28: doctor previously answered only
# "does the CLI work" — which it does with no daemon process at all — and the
# blob had no liveness field to consult. v3 adds the same probe `status` uses:
# top-level `alive` / `probe_state` / `pid` (DaemonStatus wire fields). The
# liveness probe is local (socket ping + socket-file/port observations), zero
# ws side effects, so it keeps the §9.4 contract.
SCHEMA_VERSION = 3

# Backends in this preference order are eligible to be `recommended`.
# Driven by spec §5.2's `recommended` field: choose the lowest ux_cost available.
_UX_COST_RANK = {
    "none": 0,
    "banner": 1,
    "extension-permission": 2,
}


async def doctor(cfg: Config, *, backend: str | None = None, probe_ws: bool = False) -> dict:
    """Build the locked doctor JSON object.

    `backend=None` → probe all backends. `backend="cdp"` → probe just that one
    but still emit the full shape (other entries get the canonical 'unknown'
    record with available=false).

    Liveness first (issue #28): the blob carries the same probe `status` uses
    (``alive`` / ``probe_state`` / ``pid``) so skill-side checks can fail on a
    down daemon. Run before the backend gather — its socket observations are
    sequential I/O. Zero ws side effects either way.
    """
    if probe_ws:
        # Honest: v0.1 doesn't implement the opt-in handshake. We surface the
        # flag rather than silently ignoring it — see spec §5.2's contract.
        raise UserError(
            "--probe-ws is not implemented in v0.1; remove the flag for default "
            "zero-side-effect doctor (planned for v0.2)"
        )

    if backend is not None and backend not in names():
        raise UserError(
            f"unknown backend {backend!r}; known: {', '.join(names())}"
        )

    st = await daemon_status_async(cfg)

    backends = all_backends(cfg)
    results = await asyncio.gather(*[
        b.probe() if backend is None or b.name == backend else _skipped(b)
        for b in backends
    ])

    return {
        "schema_version": SCHEMA_VERSION,
        # v3 (issue #28): the daemon-liveness probe, same fields as
        # `status --json`. A down daemon must be visible in doctor.
        "alive": st.alive,
        "probe_state": st.probe_state,
        "pid": st.pid,
        "recommended": _pick_recommended([_asdict(r) for r in results]),
        "backends": [_asdict(r) for r in results],
    }


# ---- helpers ---------------------------------------------------------------


async def _skipped(backend):
    """Used by doctor when --backend filters to one entry: every other backend
    still appears in output but with a canonical 'skipped, not probed' record
    (keeps schema shape stable for Skill)."""
    from .backends.base import DoctorResult

    return DoctorResult(
        name=backend.name,
        available=False,
        ws_url=None,
        detail="skipped (--backend filter)",
        ux_warning=None,
        needs_user_action=None,
        ux_cost=backend.ux_cost,
    )


def _asdict(r) -> dict:
    """Normalize a DoctorResult to the locked schema dict.

    We do NOT use dataclasses.asdict directly so any future field addition
    fails this function — that's the schema_version=1 trip-wire.

    `extras` (v0.5) is a per-backend free-form sub-dict. It's part of the
    serialized output so install-wizard contracts can read per-backend
    details from skill code. Empty dict = no extras (e.g. env / cdp).
    """
    return {
        "name": r.name,
        "available": r.available,
        "ws_url": r.ws_url,
        "detail": r.detail,
        "ux_warning": r.ux_warning,
        "needs_user_action": r.needs_user_action,
        "ux_cost": r.ux_cost,
        "extras": dict(r.extras) if getattr(r, "extras", None) else {},
    }


def _pick_recommended(entries: list[dict]) -> str | None:
    """Pick the available backend with the lowest UX cost.

    Tie-break: registry order (env before cdp before extension)
    via Python's stable `min`.

    v0.5.3 REVIEW.md F-10: dropped the `!= "extension"` exclusion. v0.1 had
    it because extension was hard-coded `available=false`; v0.4 shipped the
    backend with real `available=true` and the exclusion became a silent
    "this backend is never recommended even when it works" stale rule.
    `_UX_COST_RANK["extension-permission"]` = 2 — naturally ranks below
    "none" (0) and "banner" (1), above "popup-per-ws+banner" (3). So if
    extension is the only available backend, it gets recommended; if cdp
    is also available, cdp still wins on UX cost.
    """
    candidates = [e for e in entries if e["available"]]
    if not candidates:
        return None
    return min(
        candidates,
        key=lambda e: _UX_COST_RANK.get(e["ux_cost"], 99),
    )["name"]


def _needs_action(backend_name: str) -> str | None:
    """The static install-wizard hint for list-backends (no probing).

    Mirrors the actionable hint each backend would put in its doctor entry.
    Centralized here so Skill can render the chooser without a probe.

    v0.5.3 REVIEW.md F-11:
      - `extension` row updated from the stale "planned v0.4" placeholder
        to the v0.4-shipped install path.
    """
    if backend_name == "cdp":
        # Doctor probes the daemon-wide config, where `endpoint` is always None
        # (it is per-session), so this row can only ever speak for a local
        # browser. `--attach=<url>` reachability is checked when the session
        # opens, not here.
        return "launch Chrome with --remote-debugging-port=9222 (or use launch-chrome)"
    if backend_name == "extension":
        return ("load the unpacked extension from browserwright-daemon/chrome-extension/ "
                "(chrome://extensions/ → enable Developer mode → Load unpacked); "
                "or run `browserwright install` option 3")
    return None
