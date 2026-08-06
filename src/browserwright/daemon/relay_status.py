"""The relay's ``/__status__`` endpoint, fetched from one place.

The extension relay answers a plain HTTP ``GET /__status__`` with a JSON blob
describing itself and every connected extension (``extensions``,
``install_ids``, ``extension_details``, ``daemon_version``). Two Layer 1 callers
want it — ``backends/extension.py`` (doctor probe) and ``cli version --check``
(version-drift report) — and each had grown its own httpx call with its own
timeout and its own proxy handling.

``trust_env=False`` + ``mounts={}`` is the load-bearing part: loopback traffic
must never go through the user's ``HTTPS_PROXY`` / ``ALL_PROXY``, and httpx
honours those by default. Getting that wrong turns "the relay is up" into a
confusing proxy error, which is exactly the bug a shared helper prevents from
being reintroduced in the next caller.
"""
from __future__ import annotations

import httpx


def status_url(host: str, port: int) -> str:
    return f"http://{host}:{port}/__status__"


async def fetch(host: str, port: int, *, timeout: float = 2.0) -> httpx.Response:
    """GET the relay status. Raises httpx errors — the caller classifies them.

    Used by the doctor probe, which needs to tell "connection refused" (no
    daemon) apart from every other failure.
    """
    async with httpx.AsyncClient(
        timeout=timeout, trust_env=False, mounts={},
    ) as client:
        return await client.get(status_url(host, port))


def fetch_json(host: str, port: int, *, timeout: float = 1.0) -> dict | None:
    """Blocking best-effort fetch: the parsed dict, or ``None`` for any failure.

    For callers that only want to enrich their output when the relay happens to
    be up (``version --check``) and must never fail because it isn't.
    """
    try:
        with httpx.Client(timeout=timeout, trust_env=False, mounts={}) as client:
            resp = client.get(status_url(host, port))
        if resp.status_code != 200:
            return None
        payload = resp.json()
        return payload if isinstance(payload, dict) else None
    except Exception:  # noqa: BLE001 — any failure means "no status", by design
        return None
