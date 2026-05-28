"""Transparent smart waiting for Playwright ``Page.goto``.

Browserwright agents already know Playwright, so navigation should keep the
same surface while avoiding Playwright's SPA-hostile default ``wait_until=load``.
This module patches Page instances in place: callers can keep using
``page.goto(...)`` and still receive the normal Playwright ``Response | None``.
"""
from __future__ import annotations

import time
import types
from datetime import timedelta
from typing import Any

from ..errors import PageLoadFailed


_PATCHED = "_bw_smart_goto"
_ORIG_GOTO = "_bw_orig_goto"
_CONTEXT_PATCHED = "_bw_smart_new_page"
_ORIG_NEW_PAGE = "_bw_orig_new_page"
_STABLE_WINDOW_MS = 1500
_DEFAULT_TIMEOUT_MS = 60_000
_DOMCONTENTLOADED_TIMEOUT_MS = 10_000


def patch_context_pages(context: Any) -> None:
    """Patch existing pages and future ``context.new_page()`` results."""
    for page in list(getattr(context, "pages", []) or []):
        patch_page_goto(page)
    if getattr(context, _CONTEXT_PATCHED, False):
        return

    orig_new_page = context.new_page

    def new_page(*args: Any, **kwargs: Any) -> Any:
        page = orig_new_page(*args, **kwargs)
        patch_page_goto(page)
        return page

    try:
        setattr(context, _ORIG_NEW_PAGE, orig_new_page)
        setattr(context, "new_page", new_page)
        setattr(context, _CONTEXT_PATCHED, True)
    except Exception:  # noqa: BLE001 - best effort; returned pages still patch.
        return


def patch_page_goto(page: Any) -> Any:
    """Replace one Playwright Page instance's ``goto`` with smart waiting."""
    if getattr(page, _PATCHED, False):
        return page

    orig_goto = page.goto

    def smart_goto(self: Any, url: str, *, timeout: int | float | timedelta | None = _DEFAULT_TIMEOUT_MS,
                   wait_until: str | None = None, referer: str | None = None) -> Any:
        timeout_ms = _normalize_timeout(timeout)
        network = _NetworkMonitor(self)
        deadline = _deadline_for(timeout_ms)
        try:
            response = orig_goto(url, timeout=timeout_ms,
                                 wait_until="commit", referer=referer)
        except Exception as exc:  # noqa: BLE001 - translate Playwright failures.
            network.detach()
            raise _page_load_failed(url, "commit", exc) from exc

        _wait_for_domcontentloaded(self, _remaining_timeout_ms(deadline))
        try:
            _smart_wait_settled(self, deadline, network)
        finally:
            network.detach()
        return response

    try:
        setattr(page, _ORIG_GOTO, orig_goto)
        setattr(page, "goto", types.MethodType(smart_goto, page))
        setattr(page, _PATCHED, True)
    except Exception:  # noqa: BLE001
        return page
    return page


def _normalize_timeout(timeout: int | float | timedelta | None) -> int:
    if timeout is None:
        return _DEFAULT_TIMEOUT_MS
    if isinstance(timeout, timedelta):
        return max(0, int(timeout.total_seconds() * 1000))
    try:
        timeout_ms = int(timeout)
    except (TypeError, ValueError):
        return _DEFAULT_TIMEOUT_MS
    return timeout_ms if timeout_ms >= 0 else _DEFAULT_TIMEOUT_MS


def _deadline_for(timeout_ms: int) -> float | None:
    if timeout_ms == 0:
        return None
    return time.monotonic() + (timeout_ms / 1000.0)


def _remaining_timeout_ms(deadline: float | None) -> int:
    if deadline is None:
        return _DOMCONTENTLOADED_TIMEOUT_MS
    return max(1, int((deadline - time.monotonic()) * 1000))


def _wait_for_domcontentloaded(page: Any, remaining_timeout_ms: int) -> None:
    try:
        page.wait_for_load_state(
            "domcontentloaded",
            timeout=_bounded_timeout(remaining_timeout_ms, _DOMCONTENTLOADED_TIMEOUT_MS),
        )
    except Exception:
        pass


def _smart_wait_settled(page: Any, deadline: float | None, network: "_NetworkMonitor") -> None:
    try:
        page.evaluate(_INSTALL_MONITOR_JS)
    except Exception:
        return

    while deadline is None or time.monotonic() < deadline:
        remaining_ms = None if deadline is None else max(1, int((deadline - time.monotonic()) * 1000))
        poll_ms = 250 if remaining_ms is None else min(250, remaining_ms)
        try:
            settled = page.evaluate(_SETTLED_JS, _STABLE_WINDOW_MS)
        except Exception:
            return
        if settled or network.is_idle(_STABLE_WINDOW_MS):
            return
        try:
            page.wait_for_timeout(poll_ms)
        except Exception:
            return


def _bounded_timeout(total_ms: int, cap_ms: int) -> int:
    if total_ms == 0:
        return cap_ms
    return max(1, min(int(total_ms), cap_ms))


def _page_load_failed(url: str, phase: str, exc: BaseException) -> PageLoadFailed:
    msg = str(exc)
    exc_type = type(exc).__name__
    lower = msg.lower()
    if "timeout" in lower or exc_type == "TimeoutError":
        return PageLoadFailed(
            url,
            phase,
            fix="site did not respond at commit; verify it with http_get(url) or retry",
        )
    if "net::" in msg or "ssl" in lower or "name_not_resolved" in lower:
        return PageLoadFailed(
            url,
            "network",
            fix="check the URL and network; use http_get(url) to verify the site is reachable",
        )
    return PageLoadFailed(
        url,
        "network",
        fix="check the URL and network; use http_get(url) to verify the site is reachable",
    )


class _NetworkMonitor:
    def __init__(self, page: Any) -> None:
        self.page = page
        self.inflight = 0
        self.last_activity = time.monotonic()

        def on_request(*_args: Any) -> None:
            self.inflight += 1
            self.last_activity = time.monotonic()

        def on_done(*_args: Any) -> None:
            self.inflight = max(0, self.inflight - 1)
            self.last_activity = time.monotonic()

        self._handlers = {
            "request": on_request,
            "requestfinished": on_done,
            "requestfailed": on_done,
        }
        for event, handler in self._handlers.items():
            try:
                page.on(event, handler)
            except Exception:
                pass

    def is_idle(self, stable_window_ms: int) -> bool:
        quiet_s = stable_window_ms / 1000.0
        return self.inflight == 0 and (time.monotonic() - self.last_activity) >= quiet_s

    def detach(self) -> None:
        for event, handler in self._handlers.items():
            try:
                self.page.off(event, handler)
            except Exception:
                pass


_INSTALL_MONITOR_JS = """
() => {
  const w = window;
  const now = Date.now();
  if (!w.__bwSmartGoto) {
    const state = { lastMutation: now };
    try {
      const observer = new MutationObserver(() => { state.lastMutation = Date.now(); });
      observer.observe(document.documentElement || document, {
        childList: true,
        subtree: true,
        attributes: true,
        characterData: true,
      });
      state.observer = observer;
    } catch (e) {}
    w.__bwSmartGoto = state;
  }
  return true;
}
"""


_SETTLED_JS = """
(stableWindowMs) => {
  const state = window.__bwSmartGoto || {};
  const now = Date.now();
  const lastMutation = state.lastMutation || now;
  return document.readyState === "complete" &&
    (now - lastMutation) >= stableWindowMs;
}
"""
