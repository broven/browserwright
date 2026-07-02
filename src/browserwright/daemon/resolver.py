"""Mode A URL resolver.

Spec §5.1: walk the backend chain, return the first that yields a ResolveResult,
otherwise aggregate per-backend failure reasons and raise Unavailable. When
--backend is explicit, only that one backend runs (no fallback) — that's H10.

Importantly: resolve() never opens a ws. It only does HTTP discovery and
filesystem reads. The Skill opens the ws downstream. (§2.3 banner sync.)
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
    #   `extension` — LOCAL_RELAY kind, requires daemon-serve to be the
    #       ws server; `resolve()` is meaningless for Mode A.
    # It still works when `--backend extension` is explicit (the branch
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
