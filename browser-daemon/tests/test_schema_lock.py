"""Schema lock — doctor JSON shape contract (v0.5.3 / REVIEW.md F-1+F-2).

design-v2.md §5.2 calls `schema_version` a hard contract. Reviewer-1 found
v0.5 silently extended `ux_cost` (added `"auth-required"`) and `DoctorResult`
(added `extras` field) without bumping the version — and the existing
test fixtures were edited to accept the additions, masking the breach
(`KNOWN_UX_COSTS` and `EXPECTED_BACKEND_KEYS` were both updated in lockstep).

This test exists to make any future schema change a **deliberate** act:

  - The expected shape is frozen-dict / frozenset literals here, NOT the
    enum / dataclass under test. If the production code adds a key or a
    new enum value, the comparison fails — the fix requires editing THIS
    file's literal AND bumping `SCHEMA_VERSION` in the same commit. That's
    the discipline F-1+F-2 violated.
  - Test naming makes the trip-wire obvious. `test_schema_v2_*` reads as
    "this asserts the v2 contract, not v1." When the v3 jump comes,
    rename + relax v2 (with a v1→v2 compat decision documented).

What this test is NOT: a check that the schema is "correct." Just that
it doesn't drift unobserved. Whether to bump major/minor/patch on a given
field add is a spec-level call; the test surfaces the question.
"""
from __future__ import annotations

import pytest

from browser_daemon.backends import base
from browser_daemon import doctor as doctor_mod
from browser_daemon.config import load


# Frozen truth tables for v2. DO NOT loosen these without also bumping
# `doctor_mod.SCHEMA_VERSION` and rewriting this file's expectations.
V2_UX_COST_ENUM = frozenset({
    "none",
    "banner",
    "popup-per-ws+banner",
    "extension-permission",
    "auth-required",  # added v2 for the cloud backend
})

V2_BACKEND_ENTRY_KEYS = frozenset({
    "name",
    "available",
    "ws_url",
    "detail",
    "ux_warning",
    "needs_user_action",
    "ux_cost",
    "extras",  # added v2 for cloud install-wizard mirror pattern
})

V2_TOP_LEVEL_KEYS = frozenset({"schema_version", "recommended", "backends"})


# v0.5.3 F-6: BrowserDaemon.* namespace lock. Every method below must be
# documented in design-v2.md §6.4 AND dispatched in proxy.py's
# `_handle_browserdaemon`. Adding a new method = update this set, update
# the spec, write a test for the dispatch.
V2_BROWSER_DAEMON_METHODS = frozenset({
    "BrowserDaemon.getActiveTab",
    "BrowserDaemon.getBackendInfo",
    "BrowserDaemon.uiState",
    "BrowserDaemon.subscribeFocus",
    "BrowserDaemon.unsubscribeFocus",
    "BrowserDaemon.disconnect",
    "BrowserDaemon.version",
    "BrowserDaemon.stats",
    # v0.5.4 — extension backend only; -32601 on other backends.
    "BrowserDaemon.attachActiveTab",
    # Phase B: extension-backend-only verbs.
    "BrowserDaemon.openBackgroundTab",
    "BrowserDaemon.closeTab",
})


# ---- enum lock ------------------------------------------------------------


def test_schema_v2_ux_cost_enum_is_exactly_the_frozen_set():
    """If you add or remove a `UxCost` value: update V2_UX_COST_ENUM AND
    bump SCHEMA_VERSION. Past failure mode (F-1): added "auth-required"
    without bumping; reviewer caught silent drift."""
    # `Literal[...]` exposes the union members via __args__.
    actual = frozenset(base.UxCost.__args__)  # type: ignore[attr-defined]
    extra = actual - V2_UX_COST_ENUM
    missing = V2_UX_COST_ENUM - actual
    assert not extra and not missing, (
        f"UxCost enum drift detected — extra={extra}, missing={missing}. "
        f"Update tests/test_schema_lock.py V2_UX_COST_ENUM AND bump "
        f"doctor.SCHEMA_VERSION."
    )


def test_schema_v2_doctor_result_has_exactly_the_frozen_field_set():
    """If you add/remove DoctorResult fields: update V2_BACKEND_ENTRY_KEYS
    AND bump SCHEMA_VERSION. Past failure mode (F-2): added "extras"
    without bumping; existing tests masked the breach."""
    actual = frozenset(base.DoctorResult.__dataclass_fields__.keys())
    extra = actual - V2_BACKEND_ENTRY_KEYS
    missing = V2_BACKEND_ENTRY_KEYS - actual
    assert not extra and not missing, (
        f"DoctorResult field drift — extra={extra}, missing={missing}. "
        f"Update tests/test_schema_lock.py V2_BACKEND_ENTRY_KEYS AND bump "
        f"doctor.SCHEMA_VERSION."
    )


def test_schema_version_is_2_in_v053():
    """Sanity: SCHEMA_VERSION reflects the v2 jump. When v3 lands this
    becomes `== 3` AND the V2_* dictionaries get renamed/updated."""
    assert doctor_mod.SCHEMA_VERSION == 2


# ---- doctor output shape -------------------------------------------------


@pytest.mark.asyncio
async def test_doctor_output_schema_version_field_matches_constant():
    """The output's `schema_version` key MUST mirror the module constant.
    Catches an old-style schema reader that hard-coded 1."""
    out = await doctor_mod.doctor(load(env={}))
    assert out["schema_version"] == doctor_mod.SCHEMA_VERSION


@pytest.mark.asyncio
async def test_doctor_top_level_keys_are_exactly_frozen_set():
    out = await doctor_mod.doctor(load(env={}))
    actual = frozenset(out.keys())
    assert actual == V2_TOP_LEVEL_KEYS, (
        f"doctor top-level keys drifted — expected {V2_TOP_LEVEL_KEYS}, "
        f"got {actual}. Update V2_TOP_LEVEL_KEYS AND bump SCHEMA_VERSION.")


@pytest.mark.asyncio
async def test_doctor_every_backend_entry_has_exactly_v2_key_set():
    out = await doctor_mod.doctor(load(env={}))
    for entry in out["backends"]:
        actual = frozenset(entry.keys())
        assert actual == V2_BACKEND_ENTRY_KEYS, (
            f"backend {entry['name']!r} entry keys drifted — "
            f"expected {V2_BACKEND_ENTRY_KEYS}, got {actual}.")


@pytest.mark.asyncio
async def test_doctor_every_ux_cost_value_is_in_v2_enum():
    """Production probes must only emit ux_cost values the v2 enum
    recognizes. A backend that accidentally emits an unknown string is
    a schema breach — surface it here."""
    out = await doctor_mod.doctor(load(env={}))
    for entry in out["backends"]:
        assert entry["ux_cost"] in V2_UX_COST_ENUM, (
            f"backend {entry['name']!r} emitted ux_cost={entry['ux_cost']!r}, "
            f"not in v2 enum {V2_UX_COST_ENUM}.")


# ---- v0.5.3 F-6: BrowserDaemon.* namespace lock --------------------------


def test_schema_v2_browser_daemon_dispatch_matches_frozen_set():
    """REVIEW.md F-6: undocumented methods drifted into proxy.py without
    matching spec §6.4 entries. This test reads the dispatch by scanning
    `_handle_browserdaemon` source for `method == "..."` literals and
    asserts they equal V2_BROWSER_DAEMON_METHODS. Adding a method without
    updating both this set AND design-v2.md §6.4 fails the build.

    The implementation choice (regex over source rather than a registered
    dispatch table) is deliberate: routing stays a single readable
    if-chain in proxy.py — easier to scan during review than a dict + a
    decorator framework. The cost is this somewhat clever test.
    """
    import re
    from pathlib import Path

    proxy_src = Path(
        "src/browser_daemon/server/proxy.py").read_text()
    # All `method == "BrowserDaemon.XXX"` strings in the dispatch.
    found = set(re.findall(
        r'method == "(BrowserDaemon\.[A-Za-z]+)"', proxy_src))
    extra = found - V2_BROWSER_DAEMON_METHODS
    missing = V2_BROWSER_DAEMON_METHODS - found
    assert not extra and not missing, (
        f"BrowserDaemon.* dispatch drift — extra={extra}, missing={missing}. "
        f"Update tests/test_schema_lock.py V2_BROWSER_DAEMON_METHODS AND "
        f"design-v2.md §6.4 to match production.")


@pytest.mark.asyncio
async def test_browser_daemon_dispatch_uses_v2_lockable_methods_only(monkeypatch):
    """Smoke: drive every documented method through a real Router and
    assert each gets a non-empty response (not -32601 'unknown method')."""
    import asyncio
    import json
    from browser_daemon.server.proxy import Router
    from browser_daemon.server.state import DaemonState, UpstreamPhase

    state = DaemonState(name="t", backend_name="rdp")
    state.upstream_phase = UpstreamPhase.CONNECTED
    router = Router(state)
    captured: list[dict] = []

    async def _send(text: str) -> None:
        captured.append(json.loads(text))

    async def _ensure() -> None:
        pass

    async def _disc(_reason: str) -> None:
        pass

    client = state.allocate_client("t")
    router.register_client(client.client_id, _send)
    router.bind_lifecycle(_ensure, _disc)

    # v0.5.4: BrowserDaemon.attachActiveTab is backend-conditional — when no
    # extension callback is wired (this test uses rdp), it deliberately
    # returns -32601 with a "requires the extension backend" message. The
    # smoke check below distinguishes that legitimate gating from the
    # "unknown method" -32601 the test was originally written to catch.
    for i, method in enumerate(sorted(V2_BROWSER_DAEMON_METHODS), start=1):
        await router.route_from_client(client, json.dumps({
            "id": i, "method": method,
        }))
        resps = [m for m in captured if m.get("id") == i]
        assert resps, f"no response for {method}"
        resp = resps[-1]
        if "error" in resp:
            if resp["error"]["code"] == -32601:
                msg = resp["error"].get("message", "")
                assert "unknown" not in msg.lower(), (
                    f"{method} returned -32601 'unknown method' — drift "
                    f"between V2_BROWSER_DAEMON_METHODS and dispatch")
