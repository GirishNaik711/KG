#!/usr/bin/env python3
"""Provider registry — Strategy selection by config string.

Adapters register themselves at import time. get_provider() resolves the
configured name to a concrete TrackerProvider. The core depends only on the
returned interface, never on a concrete class.
"""

from __future__ import annotations

from typing import Callable

from aah.core.version_control.config import VersionControlConfig
from aah.core.version_control.providers.base import TrackerProvider

# name -> factory(provider_config: dict) -> TrackerProvider
_REGISTRY: dict[str, Callable[[dict], TrackerProvider]] = {}


def register(name: str, factory: Callable[[dict], TrackerProvider]) -> None:
    """Register a provider factory under a config name (idempotent)."""
    _REGISTRY[name] = factory


def available() -> list[str]:
    return sorted(_REGISTRY.keys())


def get_provider(config: VersionControlConfig) -> TrackerProvider:
    """Resolve the configured provider, importing built-ins on first use."""
    # Lazy import so registration side-effects fire without a hard import cycle.
    import aah.core.version_control.providers  # noqa: F401

    name = config.provider
    if name not in _REGISTRY:
        raise KeyError(
            f"Unknown version_control provider '{name}'. "
            f"Available: {available() or '[none registered]'}"
        )
    return _REGISTRY[name](config.provider_config)
