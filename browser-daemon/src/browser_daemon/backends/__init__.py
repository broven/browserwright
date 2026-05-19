"""Backend registry.

Backends are plain Python classes registered explicitly — spec §8.5 forbids
plugin systems / dynamic loading. The order returned by `all_backends()` is
also the default fallback chain used by the resolver when `--backend` is unset.
"""
from __future__ import annotations

from typing import Callable

from .base import Backend
from .env import EnvBackend
from .rdp import RdpBackend
from .extension import ExtensionBackend
from .cloud import CloudBackend


# (name, factory) — factories take a Config and return a Backend instance.
# Order is the documented fallback order: cheapest + most explicit first.
# `cloud` requires explicit config (endpoint + auth_kind) so we register it
# at the end — `resolver.resolve()` skips it in the fallback chain via
# the same exclusion list that also covers `extension`. Users must opt
# in with `--backend cloud` or `BD_BACKEND=cloud`.
_REGISTRY: list[tuple[str, Callable[..., Backend]]] = [
    ("env", EnvBackend),
    ("rdp", RdpBackend),
    ("extension", ExtensionBackend),
    ("cloud", CloudBackend),
]


def all_backends(cfg) -> list[Backend]:
    return [factory(cfg) for _, factory in _REGISTRY]


def names() -> list[str]:
    return [name for name, _ in _REGISTRY]


def get_backend(name: str, cfg) -> Backend:
    from ..errors import UserError

    for n, factory in _REGISTRY:
        if n == name:
            return factory(cfg)
    raise UserError(
        f"unknown backend {name!r}; known: {', '.join(names())}"
    )


__all__ = ["Backend", "all_backends", "names", "get_backend"]
