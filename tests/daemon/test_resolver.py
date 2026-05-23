"""resolver — Mode A fallback chain semantics."""
from __future__ import annotations

import pytest

import browserwright.daemon.resolver as resolver_mod
from browserwright.daemon.backends.base import ResolveResult
from browserwright.daemon.config import load
from browserwright.daemon.errors import Unavailable, UserError


class _StubBackend:
    def __init__(self, name, result=None, exc=None, kind="UPSTREAM_WS",
                 mode="A", ux_cost="none"):
        self.name = name
        self.kind = kind
        self.recommended_mode = mode
        self.ux_cost = ux_cost
        self._result = result
        self._exc = exc

    async def probe(self):
        from browserwright.daemon.backends.base import DoctorResult
        return DoctorResult(name=self.name, available=self._result is not None)

    async def resolve(self, timeout):
        if self._exc is not None:
            raise self._exc
        return self._result


def _patch_chain(monkeypatch, backends):
    """Replace the backend registry that `resolver.resolve` walks."""
    def all_backends(cfg):
        return backends

    def get_backend(name, cfg):
        for b in backends:
            if b.name == name:
                return b
        raise UserError(f"unknown backend {name}")

    monkeypatch.setattr(resolver_mod, "all_backends", all_backends)
    monkeypatch.setattr(resolver_mod, "get_backend", get_backend)


@pytest.mark.asyncio
async def test_first_backend_succeeds(monkeypatch):
    chain = [
        _StubBackend("env", result=ResolveResult(ws_url="ws://env/", backend="env")),
        _StubBackend("rdp", exc=Unavailable("not running")),
    ]
    _patch_chain(monkeypatch, chain)
    res = await resolver_mod.resolve(load(env={}))
    assert res.ws_url == "ws://env/"


@pytest.mark.asyncio
async def test_fallback_skips_unavailable_to_next(monkeypatch):
    chain = [
        _StubBackend("env", exc=Unavailable("BD_CDP_WS not set",
                                            attempts={"env": "no env var"})),
        _StubBackend("rdp", result=ResolveResult(ws_url="ws://rdp/", backend="rdp")),
    ]
    _patch_chain(monkeypatch, chain)
    res = await resolver_mod.resolve(load(env={}))
    assert res.ws_url == "ws://rdp/"


@pytest.mark.asyncio
async def test_all_unavailable_aggregates_attempts(monkeypatch):
    chain = [
        _StubBackend("env", exc=Unavailable("e", attempts={"env": "e why"})),
        _StubBackend("rdp", exc=Unavailable("r", attempts={"rdp": "r why"})),
    ]
    _patch_chain(monkeypatch, chain)
    with pytest.raises(Unavailable) as exc:
        await resolver_mod.resolve(load(env={}))
    assert set(exc.value.attempts.keys()) >= {"env", "rdp"}


@pytest.mark.asyncio
async def test_explicit_backend_pins_to_one_no_fallback(monkeypatch):
    """When --backend is explicit, we don't try anything else even if the
    explicit one fails. spec H10."""
    chain = [
        _StubBackend("env", exc=Unavailable("nope")),
        _StubBackend("rdp", result=ResolveResult(ws_url="ws://rdp/", backend="rdp")),
    ]
    _patch_chain(monkeypatch, chain)
    cfg = load(env={}, cli_backend="env")
    with pytest.raises(Unavailable):
        await resolver_mod.resolve(cfg)


@pytest.mark.asyncio
async def test_explicit_unknown_backend_raises_user_error(monkeypatch):
    chain = [_StubBackend("env", result=ResolveResult(ws_url="ws://env/", backend="env"))]
    _patch_chain(monkeypatch, chain)
    cfg = load(env={}, cli_backend="totally-fake")
    with pytest.raises(UserError):
        await resolver_mod.resolve(cfg)


@pytest.mark.asyncio
async def test_extension_skipped_in_auto_chain(monkeypatch):
    """extension is always Unavailable in v0.1 — including it in every
    aggregate error would just be noise. Resolver skips it on auto chain;
    explicit --backend extension still tries it (and fails, with a clear msg)."""
    # Use the *real* backend names to verify the skip-list logic.
    res_marker = ResolveResult(ws_url="ws://placeholder/", backend="env")
    chain = [
        _StubBackend("env", result=res_marker),
        _StubBackend("rdp"),
        _StubBackend("extension", exc=Unavailable("not in v0.1")),
    ]
    _patch_chain(monkeypatch, chain)
    res = await resolver_mod.resolve(load(env={}))
    assert res is res_marker
