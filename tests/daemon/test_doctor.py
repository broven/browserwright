"""doctor — schema_version=3 lock + §9.4 zero-ws-side-effect 反测试.

The schema_version=3 contract is the public contract: Skill reads it without
existence checks, so every key must be present even when null, and the field
set must NOT change in v0.x. v3 (issue #28) added the daemon-liveness fields
``alive`` / ``probe_state`` / ``pid`` — doctor must never again report the
daemon healthy when no daemon process is running.
"""
from __future__ import annotations


import pytest

import browserwright.daemon.doctor as doctor_mod
from browserwright.daemon.backends.base import DoctorResult
from browserwright.daemon.config import load
from browserwright.daemon.errors import UserError
from browserwright.daemon.probe import DaemonStatus


# ---- schema lock -----------------------------------------------------------

EXPECTED_BACKEND_KEYS = {
    "name", "available", "ws_url", "detail",
    "ux_warning", "needs_user_action", "ux_cost", "extras",
}
EXPECTED_TOP_KEYS = {
    "schema_version", "recommended", "backends",
    # v3 (issue #28): daemon-liveness probe, same fields as `status --json`.
    "alive", "probe_state", "pid",
}
KNOWN_UX_COSTS = {"none", "banner", "extension-permission"}


@pytest.fixture(autouse=True)
def _no_live_daemon(monkeypatch):
    """Doctor's liveness probe must never reach the developer's own daemon
    during tests. Stub it to a canonical 'not running' by default; liveness
    tests call :func:`_stub_liveness` again to override."""
    _stub_liveness(monkeypatch)


def _stub_liveness(monkeypatch, *, alive=False, probe_state="not_running",
                   pid=None):
    async def _fake(cfg, *, probe=None):
        return DaemonStatus(
            alive=alive,
            probe_state=probe_state,
            pid=pid,
            port_holder_pid=None,
            version=None,
            endpoint={"transport": "unix", "path": "/dev/null"},
            facade=None,
        )

    monkeypatch.setattr(doctor_mod, "daemon_status_async", _fake)


@pytest.mark.asyncio
async def test_doctor_schema_v3_top_shape(monkeypatch):
    """Top-level shape: schema_version=3, recommended:str|null, backends:list,
    plus the v3 liveness fields alive/probe_state/pid."""
    _patch_all_unavailable(monkeypatch)
    out = await doctor_mod.doctor(load(env={}))
    assert set(out.keys()) == EXPECTED_TOP_KEYS
    from browserwright.daemon.doctor import SCHEMA_VERSION

    assert out["schema_version"] == SCHEMA_VERSION
    assert isinstance(out["backends"], list)
    assert out["recommended"] is None or isinstance(out["recommended"], str)


@pytest.mark.asyncio
async def test_doctor_blob_carries_liveness_fields(monkeypatch):
    """v3 (issue #28): a live daemon is visible in the blob — alive, its
    probe_state, and its pid. Without this, skill-side doctor checks can only
    see 'the CLI answered', which is true with no daemon running at all."""
    _stub_liveness(monkeypatch, alive=True, probe_state="ok", pid=4242)
    _patch_all_unavailable(monkeypatch)
    out = await doctor_mod.doctor(load(env={}))
    assert out["alive"] is True
    assert out["probe_state"] == "ok"
    assert out["pid"] == 4242


@pytest.mark.asyncio
async def test_doctor_blob_reports_not_running(monkeypatch):
    """v3 (issue #28): with no daemon, doctor says so — the exact condition
    the old v2 blob was silent about."""
    _patch_all_unavailable(monkeypatch)
    out = await doctor_mod.doctor(load(env={}))
    assert out["alive"] is False
    assert out["probe_state"] == "not_running"
    assert out["pid"] is None


@pytest.mark.asyncio
async def test_doctor_every_backend_has_full_key_set(monkeypatch):
    """Every backend entry must carry every locked key, even when value is null
    — spec §5.2 contract so Skill doesn't have to guard with `.get()`."""
    _patch_all_unavailable(monkeypatch)
    out = await doctor_mod.doctor(load(env={}))
    seen_names = {entry["name"] for entry in out["backends"]}
    assert seen_names == {"cdp", "extension"}
    for entry in out["backends"]:
        assert set(entry.keys()) == EXPECTED_BACKEND_KEYS, (
            f"backend {entry['name']!r} schema drift: {set(entry.keys()) ^ EXPECTED_BACKEND_KEYS}"
        )
        assert entry["ux_cost"] in KNOWN_UX_COSTS


@pytest.mark.asyncio
async def test_doctor_recommended_picks_lowest_ux_cost(monkeypatch):
    """When both cdp (none) and extension (extension-permission) are
    available, cdp wins on ux-cost rank."""
    _patch_each(monkeypatch, {
        "cdp":         DoctorResult("cdp", available=True, ux_cost="none"),
        "extension":   DoctorResult("extension", available=True, ux_cost="extension-permission"),
    })
    out = await doctor_mod.doctor(load(env={}))
    assert out["recommended"] == "cdp"


@pytest.mark.asyncio
async def test_doctor_recommended_none_when_nothing_available(monkeypatch):
    _patch_all_unavailable(monkeypatch)
    out = await doctor_mod.doctor(load(env={}))
    assert out["recommended"] is None


# ---- §9.4 反测试: zero ws side effects --------------------------------------

@pytest.mark.asyncio
async def test_doctor_default_opens_no_ws(monkeypatch):
    """The cardinal rule. doctor() must not call websockets.connect, even
    indirectly — every backend's probe() is contractually side-effect-free."""
    import websockets

    calls = []

    async def boom(*a, **kw):
        calls.append(a)
        raise AssertionError("doctor() opened a ws — that's a contract violation")

    monkeypatch.setattr(websockets, "connect", boom, raising=False)

    _patch_all_unavailable(monkeypatch)
    out = await doctor_mod.doctor(load(env={}))
    assert calls == []
    from browserwright.daemon.doctor import SCHEMA_VERSION

    assert out["schema_version"] == SCHEMA_VERSION


# ---- --probe-ws opt-in not implemented (clean error, not silent ignore) ----

@pytest.mark.asyncio
async def test_doctor_probe_ws_flag_explicitly_rejects_in_v01(monkeypatch):
    _patch_all_unavailable(monkeypatch)
    with pytest.raises(UserError):
        await doctor_mod.doctor(load(env={}), probe_ws=True)


# ---- --backend filter keeps schema shape -----------------------------------

@pytest.mark.asyncio
async def test_doctor_backend_filter_keeps_full_shape(monkeypatch):
    _patch_each(monkeypatch, {
        "cdp":         DoctorResult("cdp", available=True, ux_cost="none"),
        "extension":   DoctorResult("extension", available=False, ux_cost="extension-permission"),
    })
    out = await doctor_mod.doctor(load(env={}), backend="cdp")
    # all backends still appear, but the non-cdp ones say "skipped"
    names = {e["name"] for e in out["backends"]}
    assert names == {"cdp", "extension"}
    other_entries = [e for e in out["backends"] if e["name"] != "cdp"]
    for e in other_entries:
        assert e["available"] is False
        assert "skipped" in e["detail"]


# ---- helpers ---------------------------------------------------------------


def _patch_all_unavailable(monkeypatch):
    _patch_each(monkeypatch, {
        "cdp":         DoctorResult("cdp", available=False, ux_cost="none"),
        "extension":   DoctorResult("extension", available=False, ux_cost="extension-permission"),
    })


def _patch_each(monkeypatch, probes: dict[str, DoctorResult]):
    """Replace doctor.all_backends() with stub backends, each returning a
    canned DoctorResult on probe(). resolve() raises (doctor doesn't use it)."""

    class _Stub:
        def __init__(self, dr):
            self.name = dr.name
            self.kind = "UPSTREAM_WS"
            self.recommended_mode = "A"
            self.ux_cost = dr.ux_cost
            self._dr = dr

        async def probe(self):
            return self._dr

        async def resolve(self, timeout):
            raise AssertionError("doctor should never call resolve()")

    stubs = [_Stub(probes[name]) for name in
             ["cdp", "extension"]]
    monkeypatch.setattr(doctor_mod, "all_backends", lambda cfg: stubs)


# ---- v0.5.3 F-10: drop stale `extension` exclusion from _pick_recommended


@pytest.mark.asyncio
async def test_recommended_picks_extension_when_only_one_available(monkeypatch):
    """v0.5.3 F-10: previously _pick_recommended excluded extension with a
    v0.1-era 'hard-coded false' comment. v0.4+ extension is real; if it's
    the only available backend, it should be recommended."""
    _patch_each(monkeypatch, {
        "cdp":         DoctorResult("cdp", available=False, ux_cost="none"),
        "extension":   DoctorResult("extension", available=True, ux_cost="extension-permission"),
    })
    out = await doctor_mod.doctor(load(env={}))
    assert out["recommended"] == "extension"


@pytest.mark.asyncio
async def test_recommended_cdp_still_beats_extension_on_ux_cost(monkeypatch):
    """Sanity: even though extension is now recommendable, cdp (none)
    still wins over extension (extension-permission) on UX cost rank."""
    _patch_each(monkeypatch, {
        "cdp":         DoctorResult("cdp", available=True, ux_cost="none"),
        "extension":   DoctorResult("extension", available=True, ux_cost="extension-permission"),
    })
    out = await doctor_mod.doctor(load(env={}))
    assert out["recommended"] == "cdp"


# ---- v0.5.3 F-11: _needs_action hints refreshed --------------------------


def test_needs_action_unknown_backend_still_none():
    """Spec sanity — unknown name returns None, doesn't fall through."""
    assert doctor_mod._needs_action("nonexistent") is None
