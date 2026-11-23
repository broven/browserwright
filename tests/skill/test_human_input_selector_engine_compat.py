"""Pin the scroll-selector-engine registration across Playwright channel shapes.

The executor hangs its call to the deadline when ``install_human_input`` cannot
register the engine, so this is a transport-compatibility contract, not a
detail.

Root cause (2026-11, browserwright 0.24.3 on a fresh ``uv tool install``):
Playwright's internal ``registerSelectorEngine`` command has no public Python
wrapper and its client spelling changed inside the ``playwright>=1.60.0`` range
this package allows:

  - ``<= 1.64``: every channel is the generic ``Channel``; the command is
    ``channel.send('registerSelectorEngine', timeout, params)``;
  - ``>= 1.65``: generic channels were replaced by per-object generated
    channels; ``send`` is gone and the command is a generated
    ``channel.registerSelectorEngine(engine)`` returning an awaitable.

The old spelling raised ``AttributeError`` on 1.65+ inside the executor's
worker (``_execute`` → ``_build_globals`` → ``_bind_live_surface`` →
``install_human_input``). That worker has no crash-to-response path, so the
thread died silently and the CLI waited until ``DeadlineExceeded`` — every call
that touched ``page``/``context``, even ``print(type(context).__name__)``,
because the executor is cold-started before the user code runs.

These tests drive the real ``install_human_input`` with a fake context, so the
pre-fix code fails on the generated-channel case with exactly that
``AttributeError`` while a real browser connection is never needed.
"""
from __future__ import annotations

import types
import weakref

import pytest

from browserwright.repl import human_input

_EXPECTED_ENGINE = {
    "name": human_input._SCROLL_ENGINE,
    "source": human_input._SCROLL_SOURCE,
    "contentScript": True,
}


class _DriverAwaitable:
    """Stand-in for the awaitable a Playwright channel method returns."""


class _GeneratedChannel:
    """Playwright >= 1.65: generated method, no generic ``send``."""

    def __init__(self) -> None:
        self.registered: list[dict] = []

    def registerSelectorEngine(self, selectorEngine: dict) -> _DriverAwaitable:
        self.registered.append(selectorEngine)
        return _DriverAwaitable()


class _LegacyChannel:
    """Playwright <= 1.64: generic ``Channel.send(method, timeout, params)``."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, object, object]] = []

    def send(self, method: str, timeout: object, params: object) -> _DriverAwaitable:
        self.calls.append((method, timeout, params))
        return _DriverAwaitable()


class _FakeContext:
    """Minimal BrowserContext: channel + the sync helper, nothing else."""

    def __init__(self, channel: object) -> None:
        self._impl_obj = types.SimpleNamespace(_channel=channel)
        self.synced: list[object] = []

    def _sync(self, awaitable: object) -> None:
        # Real `_sync` hands the coroutine to the driver's greenlet. Recording
        # it is enough: the channel double already captured the payload.
        self.synced.append(awaitable)


@pytest.fixture
def install_context(monkeypatch):
    """Return a factory that runs the real ``install_human_input`` on a double.

    ``BrowserContext`` is monkeypatched (the function imports it per call) so
    the isinstance gate admits the double instead of needing a live driver.
    ``_INSTALLED`` is forced True so the call stops after the registration
    block: the method-wrapper patching below it is process-global and belongs
    to the real-Chrome e2e suite (``e2e/test_human_input.py``).
    """
    import playwright.sync_api as sync_api

    monkeypatch.setattr(sync_api, "BrowserContext", _FakeContext)
    monkeypatch.setattr(human_input, "_INSTALLED", True)
    monkeypatch.setattr(human_input, "_SCROLL_CONTEXTS", weakref.WeakSet())

    def install(channel: object) -> _FakeContext:
        context = _FakeContext(channel)
        human_input.install_human_input(context)
        return context

    return install


def test_registers_scroll_engine_on_generated_channel(install_context):
    """Playwright >= 1.65 (no ``Channel.send``) -- the shape that caused the hang."""
    channel = _GeneratedChannel()

    context = install_context(channel)

    assert channel.registered == [_EXPECTED_ENGINE]
    assert len(context.synced) == 1
    assert context in human_input._SCROLL_CONTEXTS


def test_registers_scroll_engine_on_legacy_generic_channel(install_context):
    """Playwright <= 1.64 -- the shape the dependency floor still allows."""
    channel = _LegacyChannel()

    context = install_context(channel)

    assert channel.calls == [
        ("registerSelectorEngine", None, {"selectorEngine": _EXPECTED_ENGINE}),
    ]
    assert len(context.synced) == 1
    assert context in human_input._SCROLL_CONTEXTS
