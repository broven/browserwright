"""Pin the sampled pointer path against Playwright's mouse-move signature.

``_mouse_samples`` batches Playwright's impl-level ``Mouse.move`` calls so a
sampled approach path does not pay one public sync round-trip per point. That
impl method's third parameter changed shape inside the ``playwright>=1.60.0``
range:

  - ``<= 1.64``: ``async def move(self, x, y, steps: int = None)`` — the old
    two-argument call was fine;
  - ``>= 1.65``: ``async def move(self, x, y, steps: Optional[int])`` — ``steps``
    became mandatory, so the old call raised
    ``TypeError: Mouse.move() missing 1 required positional argument: 'steps'``
    from EVERY human click/fill (``_fill`` clicks before typing).

The compat call passes the public API's default ``steps=1`` explicitly, which
both signatures accept. These tests drive the real ``_mouse_samples`` with a
fake page whose impl mouse has each shape; on the pre-fix code the 1.65 shape
fails with exactly that TypeError, no browser connection needed.

Real-browser click/fill parity is covered by ``tests/daemon/e2e/
test_human_input.py`` (Playwright <= 1.64 on the pinned dev env) and by the
branch-install acceptance on a Playwright 1.65 host (see PR #146).
"""
from __future__ import annotations

import asyncio

from browserwright.repl import human_input


class _BudgetStub:
    """Only ``remaining`` is consumed by the sampled-move path."""

    def remaining(self) -> float:
        return 5_000.0


class _NeedsStepsMouseImpl:
    """Playwright >= 1.65 shape: ``steps`` is a required positional."""

    def __init__(self) -> None:
        self.moves: list[tuple[float, float, int]] = []

    async def move(self, x: float, y: float, steps: int) -> None:
        self.moves.append((x, y, steps))


class _OptionalStepsMouseImpl:
    """Playwright <= 1.64 shape: ``steps`` defaults to ``None``."""

    def __init__(self) -> None:
        self.moves: list[tuple[float, float, int | None]] = []

    async def move(self, x: float, y: float, steps: int | None = None) -> None:
        self.moves.append((x, y, steps))


class _FakeMouse:
    def __init__(self, impl: object) -> None:
        self._impl_obj = impl


class _FakePage:
    """Just the surface ``_mouse_samples`` touches: ``mouse`` + ``_sync``."""

    def __init__(self, impl: object) -> None:
        self.mouse = _FakeMouse(impl)

    def _sync(self, coroutine):
        return asyncio.run(coroutine)


def test_mouse_samples_supports_playwright_1_65_required_steps():
    """The 1.65 signature accepts the batch (the shape that broke click/fill)."""
    impl = _NeedsStepsMouseImpl()

    human_input._mouse_samples(
        _FakePage(impl), [(10.0, 20.0), (11.5, 21.5)], _BudgetStub())

    assert impl.moves == [(10.0, 20.0, 1), (11.5, 21.5, 1)]


def test_mouse_samples_supports_legacy_optional_steps():
    """The <= 1.64 signature still accepts the same batched call."""
    impl = _OptionalStepsMouseImpl()

    human_input._mouse_samples(
        _FakePage(impl), [(30.0, 40.0), (31.0, 41.0)], _BudgetStub())

    assert impl.moves == [(30.0, 40.0, 1), (31.0, 41.0, 1)]
