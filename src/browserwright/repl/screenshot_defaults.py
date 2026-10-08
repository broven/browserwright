"""Screenshot defaults for the executor's live Playwright surface.

Playwright's ``screenshot()`` defaults to ``caret="hide"``, which writes inline
``caret-color: transparent`` on every editable element (shadow roots and child
frames included) and restores it afterwards. Those style writes land in the
shared DOM, so page code can observe them (MutationObserver, style reads) even
though the capture itself is a protocol command.

For contexts registered here, ``Page``/``Locator``/``ElementHandle``
``screenshot()`` default to ``caret="initial"`` instead. Every other option —
path, clip, type, quality, scale, mask — keeps native behavior, and a caller
that passes ``caret`` explicitly (including ``caret="hide"``) gets exactly that.
"""
from __future__ import annotations

import functools
import weakref
from typing import Any

_CONTEXTS: weakref.WeakSet[Any] = weakref.WeakSet()
_INSTALLED = False


def _page_of(obj: Any, cls_name: str) -> Any | None:
    if cls_name == "Page":
        return obj
    if cls_name == "Locator":
        return obj.page
    # ElementHandle has no public ``page``; it keeps its owning frame without a
    # browser round-trip, including handles returned by ``evaluate_handle``.
    try:
        from playwright._impl._sync_base import mapping
        return mapping.from_impl(obj._impl_obj._frame.page)
    except Exception:  # noqa: BLE001 - unknown internals: keep native defaults.
        return None


def install_screenshot_defaults(context: Any) -> None:
    """Register ``context`` and patch the sync screenshot methods once."""
    global _INSTALLED
    from playwright.sync_api import BrowserContext, ElementHandle, Locator, Page

    if not isinstance(context, BrowserContext):
        return
    _CONTEXTS.add(context)
    if _INSTALLED:
        return
    _INSTALLED = True

    for cls in (Page, Locator, ElementHandle):
        native = cls.screenshot

        def wrap(native: Any, cls_name: str) -> Any:
            @functools.wraps(native)
            def screenshot(self: Any, *args: Any, **kwargs: Any) -> Any:
                if kwargs.get("caret") is None:
                    page = _page_of(self, cls_name)
                    if page is not None and page.context in _CONTEXTS:
                        kwargs["caret"] = "initial"
                return native(self, *args, **kwargs)
            return screenshot

        setattr(cls, "screenshot", wrap(native, cls.__name__))
