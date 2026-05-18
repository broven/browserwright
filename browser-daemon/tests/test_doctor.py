"""doctor — schema_version=1 lock + §9.4 zero-ws-side-effect 反测试.

The schema_version=1 contract is the public contract: Skill reads it without
existence checks, so every key must be present even when null, and the field
set must NOT change in v0.x.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

import browser_daemon.doctor as doctor_mod
from browser_daemon.backends.base import DoctorResult
from browser_daemon.config import load
from browser_daemon.errors import UserError


# ---- schema lock -----------------------------------------------------------

EXPECTED_BACKEND_KEYS = {
    "name", "available", "ws_url", "detail",
    "ux_warning", "needs_user_action", "ux_cost", "extras",
}
EXPECTED_TOP_KEYS = {"schema_version", "recommended", "backends"}
KNOWN_UX_COSTS = {"none", "banner", "popup-per-ws+banner",
                  "extension-permission", "auth-required"}


@pytest.mark.asyncio
async def test_doctor_schema_v1_top_shape(monkeypatch):
    """Top-level shape: schema_version=1, recommended:str|null, backends:list."""
    _patch_all_unavailable(monkeypatch)
    out = await doctor_mod.doctor(load(env={}))
    assert set(out.keys()) == EXPECTED_TOP_KEYS
    from browser_daemon.doctor import SCHEMA_VERSION

    assert out["schema_version"] == SCHEMA_VERSION
    assert isinstance(out["backends"], list)
    assert out["recommended"] is None or isinstance(out["recommended"], str)


@pytest.mark.asyncio
async def test_doctor_every_backend_has_full_key_set(monkeypatch):
    """Every backend entry must carry every locked key, even when value is null
    — spec §5.2 contract so Skill doesn't have to guard with `.get()`."""
    _patch_all_unavailable(monkeypatch)
    out = await doctor_mod.doctor(load(env={}))
    seen_names = {entry["name"] for entry in out["backends"]}
    assert seen_names == {"env", "rdp", "autoconnect", "extension", "cloud"}
    for entry in out["backends"]:
        assert set(entry.keys()) == EXPECTED_BACKEND_KEYS, (
            f"backend {entry['name']!r} schema drift: {set(entry.keys()) ^ EXPECTED_BACKEND_KEYS}"
        )
        assert entry["ux_cost"] in KNOWN_UX_COSTS


@pytest.mark.asyncio
async def test_doctor_recommended_picks_lowest_ux_cost(monkeypatch):
    """When both rdp (none) and autoconnect (popup) are available, rdp wins."""
    _patch_each(monkeypatch, {
        "env":         DoctorResult("env", available=False, ux_cost="none"),
        "rdp":         DoctorResult("rdp", available=True, ux_cost="none"),
        "autoconnect": DoctorResult("autoconnect", available=True, ux_cost="popup-per-ws+banner"),
        "extension":   DoctorResult("extension", available=False, ux_cost="extension-permission"),
        "cloud":       DoctorResult("cloud", available=False, ux_cost="none"),
    })
    out = await doctor_mod.doctor(load(env={}))
    assert out["recommended"] == "rdp"


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
    from browser_daemon.doctor import SCHEMA_VERSION

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
        "env":         DoctorResult("env", available=True, ux_cost="none"),
        "rdp":         DoctorResult("rdp", available=True, ux_cost="none"),
        "autoconnect": DoctorResult("autoconnect", available=True, ux_cost="popup-per-ws+banner"),
        "extension":   DoctorResult("extension", available=False, ux_cost="extension-permission"),
        "cloud":       DoctorResult("cloud", available=False, ux_cost="none"),
    })
    out = await doctor_mod.doctor(load(env={}), backend="rdp")
    # all 5 backends still appear, but the non-rdp ones say "skipped"
    names = {e["name"] for e in out["backends"]}
    assert names == {"env", "rdp", "autoconnect", "extension", "cloud"}
    other_entries = [e for e in out["backends"] if e["name"] != "rdp"]
    for e in other_entries:
        assert e["available"] is False
        assert "skipped" in e["detail"]


# ---- helpers ---------------------------------------------------------------


def _patch_all_unavailable(monkeypatch):
    _patch_each(monkeypatch, {
        "env":         DoctorResult("env", available=False, ux_cost="none"),
        "rdp":         DoctorResult("rdp", available=False, ux_cost="none"),
        "autoconnect": DoctorResult("autoconnect", available=False, ux_cost="popup-per-ws+banner"),
        "extension":   DoctorResult("extension", available=False, ux_cost="extension-permission"),
        "cloud":       DoctorResult("cloud", available=False, ux_cost="none"),
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
             ["env", "rdp", "autoconnect", "extension", "cloud"]]
    monkeypatch.setattr(doctor_mod, "all_backends", lambda cfg: stubs)


# ---- v0.5.3 F-10: drop stale `extension` exclusion from _pick_recommended


@pytest.mark.asyncio
async def test_recommended_picks_extension_when_only_one_available(monkeypatch):
    """v0.5.3 F-10: previously _pick_recommended excluded extension with a
    v0.1-era 'hard-coded false' comment. v0.4+ extension is real; if it's
    the only available backend, it should be recommended."""
    _patch_each(monkeypatch, {
        "env":         DoctorResult("env", available=False, ux_cost="none"),
        "rdp":         DoctorResult("rdp", available=False, ux_cost="none"),
        "autoconnect": DoctorResult("autoconnect", available=False, ux_cost="popup-per-ws+banner"),
        "extension":   DoctorResult("extension", available=True, ux_cost="extension-permission"),
        "cloud":       DoctorResult("cloud", available=False, ux_cost="auth-required"),
    })
    out = await doctor_mod.doctor(load(env={}))
    assert out["recommended"] == "extension"


@pytest.mark.asyncio
async def test_recommended_rdp_still_beats_extension_on_ux_cost(monkeypatch):
    """Sanity: even though extension is now recommendable, rdp (none)
    still wins over extension (extension-permission) on UX cost rank."""
    _patch_each(monkeypatch, {
        "env":         DoctorResult("env", available=False, ux_cost="none"),
        "rdp":         DoctorResult("rdp", available=True, ux_cost="none"),
        "autoconnect": DoctorResult("autoconnect", available=False, ux_cost="popup-per-ws+banner"),
        "extension":   DoctorResult("extension", available=True, ux_cost="extension-permission"),
        "cloud":       DoctorResult("cloud", available=False, ux_cost="auth-required"),
    })
    out = await doctor_mod.doctor(load(env={}))
    assert out["recommended"] == "rdp"


# ---- v0.5.3 F-11: _needs_action hints refreshed --------------------------


def test_needs_action_extension_hint_no_longer_says_planned_v04():
    """v0.5.3 F-11: 'planned v0.4' was stale months after v0.4 shipped."""
    hint = doctor_mod._needs_action("extension")
    assert hint is not None
    assert "planned" not in hint.lower()
    assert "v0.4" not in hint
    # Should point at the unpacked-extension install flow.
    assert "unpacked" in hint.lower() or "browser-skill install" in hint


def test_needs_action_cloud_row_exists():
    """v0.5.3 F-11: cloud was missing from _needs_action entirely."""
    hint = doctor_mod._needs_action("cloud")
    assert hint is not None
    assert "[backends.cloud]" in hint or "browser-skill install" in hint


def test_needs_action_unknown_backend_still_none():
    """Spec sanity — unknown name returns None, doesn't fall through."""
    assert doctor_mod._needs_action("nonexistent") is None
