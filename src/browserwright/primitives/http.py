"""Pure-HTTP helpers — no browser required.

``http_get`` is the canonical "I just want the bytes of a page" escape
hatch. Spec §A.2 / browser-harness pattern: pair with
``ThreadPoolExecutor`` for bulk static-page scraping (e.g. paginated
list pages) — opening a browser for every page is wasteful.
"""
from __future__ import annotations

import gzip
import os
import urllib.request
from typing import Optional


def http_get(url: str, *, headers: Optional[dict] = None,
             timeout: float = 20.0) -> str:
    """Plain HTTP GET. Decodes gzip automatically; returns text.

    When ``BROWSER_USE_API_KEY`` is set, prefers the optional ``fetch_use``
    proxy (handles bot detection / residential proxies / retries) if
    installed; otherwise falls back to stdlib ``urllib`` with a vanilla
    Mozilla UA + gzip Accept-Encoding header.
    """
    if os.environ.get("BROWSER_USE_API_KEY"):
        try:
            from fetch_use import fetch_sync  # type: ignore[import-not-found]
            return fetch_sync(
                url, headers=headers, timeout_ms=int(timeout * 1000),
            ).text
        except ImportError:
            pass

    h = {"User-Agent": "Mozilla/5.0", "Accept-Encoding": "gzip"}
    if headers:
        h.update(headers)
    req = urllib.request.Request(url, headers=h)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = r.read()
        if r.headers.get("Content-Encoding") == "gzip":
            data = gzip.decompress(data)
        return data.decode()
