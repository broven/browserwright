"""Make every Playwright timeout the agent's code sees an ``OperationTimeout``.

ADR-0014 gives an operation timeout (one Playwright call running out of its
own ``timeout=``) its own exception and exit code 8. Playwright raises its
plain ``TimeoutError``; this module turns it into ``errors.OperationTimeout``
(a subclass of it, so ``except TimeoutError`` keeps working) at the one place
every sync Playwright call returns to the caller:

- ``SyncBase._sync`` — the return path of every sync API method
  (``click``, ``fill``, ``wait_for``, ``goto``, …);
- ``EventInfo.value`` — the return path of the ``expect_*`` context managers,
  whose timeout is raised from ``__exit__`` and never passes through ``_sync``.

Converting there, rather than when Playwright builds the error, keeps
Playwright's own internals untouched: they only ever see their own class, and
the agent only ever sees ours. A timeout that is already an
``OperationTimeout`` passes through unchanged, so nested calls convert once.

Process-wide and idempotent. Installed by ``_namespace.build_globals``, which
every code path that runs agent code goes through (the executor and the
in-process heredoc).
"""
from __future__ import annotations

from typing import Any

from ..errors import OperationTimeout, PlaywrightTimeoutError

_INSTALLED = "_bw_operation_timeout"


def as_operation_timeout(exc: BaseException) -> BaseException:
    """``exc`` as an ``OperationTimeout`` if it is a plain Playwright
    timeout; anything else is returned unchanged."""
    if not isinstance(exc, PlaywrightTimeoutError) or isinstance(
            exc, OperationTimeout):
        return exc
    converted = OperationTimeout(getattr(exc, "message", None) or str(exc))
    # Keep what Playwright recorded about where the call came from.
    converted._name = getattr(exc, "_name", None)
    converted._stack = getattr(exc, "_stack", None)
    return converted.with_traceback(exc.__traceback__)


def install() -> None:
    from playwright._impl import _sync_base

    sync_base = _sync_base.SyncBase
    if getattr(sync_base, _INSTALLED, False):
        return

    orig_sync = sync_base._sync

    def _sync(self: Any, coro: Any) -> Any:
        __tracebackhide__ = True
        try:
            return orig_sync(self, coro)
        except PlaywrightTimeoutError as exc:
            converted = as_operation_timeout(exc)
            if converted is exc:
                raise
            raise converted from None

    orig_value = _sync_base.EventInfo.value

    def value(self: Any) -> Any:
        try:
            return orig_value.fget(self)
        except PlaywrightTimeoutError as exc:
            converted = as_operation_timeout(exc)
            if converted is exc:
                raise
            raise converted from None

    sync_base._sync = _sync
    _sync_base.EventInfo.value = property(value)
    setattr(sync_base, _INSTALLED, True)
