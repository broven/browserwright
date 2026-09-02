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
            if _looks_loaded(self):
                response = None
            else:
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


def _looks_loaded(page: Any) -> bool:
    """Detect successful navigations masked by commit watcher races.

    Some redirects/client transitions can leave Playwright's commit wait in an
    error state even after the document is usable. Treat any probe failure as
    not loaded so true failures still follow the existing PageLoadFailed path.
    """
    try:
        url = page.url
        if not url or url == "about:blank":
            return False
        ready_state = page.evaluate("() => document.readyState")
        return ready_state != "loading"
    except Exception:
        return False


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


# Navigation failure buckets. These are the `reason` field of PageLoadFailed —
# an agent reads it to decide what to do next, so every bucket must point at a
# DIFFERENT next action. Two buckets that share a fix are a bug: this table
# used to collapse everything non-timeout into "network", which told users to
# check their connection while the real failure was in the CDP/relay layer.
#
# The concrete accident this replaces: the extension caps every
# `chrome.debugger.sendCommand` at 9000ms (`DEBUGGER_COMMAND_TIMEOUT_MS` in
# chrome-extension/background.js) and reports the breach as "... timed out
# after 9000ms ...". That string contains "timed out" but NOT "timeout", so it
# missed the timeout check and fell into the second, identical branch —
# `network`. A whole crawl's worth of "the extension's navigate budget expired
# on a slow page" was reported to the operator as "check your network".
_REASON_TIMEOUT = "timeout"
_REASON_NETWORK = "network"
_REASON_EXT_BUDGET = "extension-budget"
_REASON_NAV_INTERRUPTED = "navigation-interrupted"
_REASON_TARGET_CLOSED = "target-closed"
_REASON_FRAME_DETACHED = "frame-detached"
_REASON_CDP = "cdp-transport"
_REASON_UNKNOWN = "unknown"

_DETAIL_MAX = 300

# Ordered most-specific-first; the first entry whose needle appears in the
# lowercased message wins. Transport buckets deliberately sit ABOVE the generic
# timeout bucket: a relay/extension budget that expires is NOT the site failing
# to respond, and saying so sends the operator to the wrong layer.
_CLASSIFIERS: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    (
        _REASON_EXT_BUDGET,
        "NOT a network problem: the browserwright extension caps every "
        "chrome.debugger command at 9s (DEBUGGER_COMMAND_TIMEOUT_MS in "
        "chrome-extension/background.js — a constant, not an env var), and "
        "this navigation took longer to commit. The navigation may still be "
        "completing in Chrome. Heavy SPAs routinely exceed it; navigate from a "
        "fresh tab (context.new_page()) instead of reusing one parked on a "
        "heavy page, and retry",
        ("chrome.debugger.sendcommand timed out", "-32001",
         "chrome.debugger.attach timed out", "chrome.debugger.detach timed out"),
    ),
    (
        # net::ERR_ABORTED is NOT a network condition: Chrome emits it when a
        # navigation is cancelled — superseded by another goto, turned into a
        # download, or killed by the page itself. Bucketing it as "network"
        # sends the user to check their connection for a race they own.
        _REASON_NAV_INTERRUPTED,
        "navigation was cancelled or superseded (download, redirect, or a "
        "competing goto on the same page); ensure only one navigation runs per "
        "page at a time, then retry page.goto(url)",
        ("net::err_aborted", "interrupted by another navigation",
         "navigation was cancel"),
    ),
    (
        _REASON_NETWORK,
        "check the URL and network; use http_get(url) to verify the site is "
        "reachable",
        ("net::", "ssl", "name_not_resolved", "err_internet_disconnected"),
    ),
    (
        _REASON_TARGET_CLOSED,
        "the tab/context backing this session went away mid-navigation; "
        "re-acquire the page (page() / `session reset <id>`) before retrying, "
        "and check whether the tab was closed by hand or by another session",
        ("target closed", "target page, context or browser has been closed",
         "browser has been closed", "page has been closed", "page was closed",
         "session closed", "has been closed"),
    ),
    (
        _REASON_FRAME_DETACHED,
        "the frame was detached mid-navigation (usually a same-page rewrite or "
        "an iframe teardown); retry page.goto(url) on a freshly acquired frame",
        ("frame was detached", "frame has been detached", "detached frame",
         "execution context was destroyed"),
    ),
    (
        _REASON_CDP,
        "the CDP path between the daemon, the extension relay and Chrome "
        "failed — a transport fault, NOT the site. Check `browserwright "
        "doctor` and the daemon log; if it repeats, recycle the session",
        ("relay send failed", "extension relay", "extension reconnected",
         "protocol error", "websocket", "ws closed", "no close frame",
         "connection closed", "chrome.debugger", "debugger is not attached"),
    ),
)


def _detail_for(exc: BaseException) -> str:
    """Original exception type + first message line, bounded.

    Playwright appends a multi-line call log to most errors; the first line is
    the part that identifies the failure. Everything below it is noise, but the
    type name never is — a bare message loses the difference between a
    TimeoutError and a transport error that happens to mention a timeout.
    """
    lines = str(exc).strip().splitlines()
    head = lines[0].strip() if lines else ""
    if len(head) > _DETAIL_MAX:
        head = head[: _DETAIL_MAX - 1] + "\u2026"
    return f"{type(exc).__name__}: {head}" if head else type(exc).__name__


def _classify(exc: BaseException) -> tuple[str, str]:
    """Map a navigation exception to (reason, fix)."""
    lower = str(exc).lower()
    for reason, fix, needles in _CLASSIFIERS:
        if any(needle in lower for needle in needles):
            return reason, fix
    if "timeout" in lower or "timed out" in lower or type(exc).__name__ == "TimeoutError":
        return (
            _REASON_TIMEOUT,
            "site did not respond at commit; verify it with http_get(url) or retry",
        )
    return (
        _REASON_UNKNOWN,
        "unrecognised navigation failure — do NOT assume it is the network; "
        "read the exception detail in this message, then retry page.goto(url) "
        "once and check the daemon log if it repeats",
    )


def _page_load_failed(url: str, phase: str, exc: BaseException) -> PageLoadFailed:
    reason, fix = _classify(exc)
    # `phase` ("commit") is only meaningful for the timeout bucket, where it
    # says how far the navigation got. Every other bucket names its own cause.
    if reason == _REASON_TIMEOUT:
        reason = phase or _REASON_TIMEOUT
    return PageLoadFailed(url, reason, fix=fix, detail=_detail_for(exc))


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
