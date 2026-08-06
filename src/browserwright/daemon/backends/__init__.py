"""Backend registry.

Backends are plain Python classes registered explicitly — spec §8.5 forbids
plugin systems / dynamic loading. The order returned by `all_backends()` is
also the default fallback chain used by the resolver when `--backend` is unset.
"""
from __future__ import annotations

from typing import Callable

from .base import Backend
from .cdp import CdpBackend
from .extension import ExtensionBackend


# (name, factory) — factories take a Config and return a Backend instance.
# Order is the documented fallback order: cheapest + most explicit first.
#
# `env` used to sit ahead of `cdp` here. It is gone (#38): it was the same
# real-CDP backend differing only in where the ws URL came from, and that is
# now a per-session field rather than a separate backend id.
_REGISTRY: list[tuple[str, Callable[..., Backend]]] = [
    ("cdp", CdpBackend),
    ("extension", ExtensionBackend),
]


def all_backends(cfg) -> list[Backend]:
    return [factory(cfg) for _, factory in _REGISTRY]


def names() -> list[str]:
    return [name for name, _ in _REGISTRY]

def kind_for(name: str) -> str | None:
    """The ``BackendKind`` of a registered backend (class attribute, no
    instantiation), or ``None`` for unknown/unresolved names like ``"auto"``."""
    for n, factory in _REGISTRY:
        if n == name:
            return getattr(factory, "kind", None)
    return None


def get_backend(name: str, cfg) -> Backend:
    from ..errors import UserError

    for n, factory in _REGISTRY:
        if n == name:
            return factory(cfg)
    raise UserError(
        f"unknown backend {name!r}; known: {', '.join(names())}"
    )


__all__ = ["Backend", "all_backends", "names", "get_backend", "kind_for"]
