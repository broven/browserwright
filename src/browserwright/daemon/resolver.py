"""Upstream CDP URL resolver.

Walk the backend chain, return the first that yields a ResolveResult,
otherwise aggregate per-backend failure reasons and raise Unavailable. When
a backend is pinned (cfg.backend), only that one backend runs (no fallback).

Used by the daemon itself (`server/listener.py` shared-upstream open,
`server/facade.py` upstream resolution). It is not a user-facing CLI verb —
the old one-shot `browserwright-daemon url` (Mode A) was removed.

Importantly: resolve() never opens a ws. It only does HTTP discovery and
filesystem reads. The caller opens the ws downstream.
"""
from __future__ import annotations

from .backends import all_backends, get_backend
from .config import Config
from .errors import Unavailable


async def resolve(cfg: Config):
    """Return a ResolveResult for the first backend that succeeds.

    cfg.backend pinning behavior:
    - None  -> try every backend in registry order (env > rdp), then bail.
              `extension` is deliberately excluded from the auto-fallback
              (see _CHAIN_OPT_OUT below).
    - str   -> try only that one
    """
    if cfg.backend is not None:
        backend = get_backend(cfg.backend, cfg)
        try:
            return await backend.resolve(cfg.timeout)
        except Unavailable:
            raise  # explicit backend: surface attempts as-is
    # Auto chain. One backend is explicitly NOT in the auto-fallback:
    #   `extension` — LOCAL_RELAY kind: the daemon itself is the ws server,
    #       so there is no external CDP endpoint for `resolve()` to discover.
    # It still surfaces when the backend is pinned explicitly (the branch
    # above), it's just absent from auto-fallback.
    attempts: dict[str, str] = {}
    _CHAIN_OPT_OUT = {"extension"}
    for backend in all_backends(cfg):
        if backend.name in _CHAIN_OPT_OUT:
            continue
        try:
            return await backend.resolve(cfg.timeout)
        except Unavailable as e:
            # Preserve each backend's specific reason
            if e.attempts:
                attempts.update(e.attempts)
            else:
                attempts[backend.name] = str(e)
    raise Unavailable(
        "no backend could resolve a CDP WebSocket URL",
        attempts=attempts,
    )
