"""Registry of strategy implementations (name -> class)."""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType

from adaptive_quant.quant.strategies.base import Strategy

_REGISTRY: dict[str, type[Strategy]] = {}


def register[S: type[Strategy]](cls: S) -> S:
    name = cls.implementation
    if name in _REGISTRY and _REGISTRY[name] is not cls:
        raise ValueError(f"strategy implementation {name!r} registered twice")
    _REGISTRY[name] = cls
    return cls


def registry() -> Mapping[str, type[Strategy]]:
    """All implementations (importing the catalogue registers them)."""
    from adaptive_quant.quant.strategies import catalogue  # noqa: F401 - registers classes

    return MappingProxyType(_REGISTRY)


def create(
    implementation: str, strategy_id: str | None = None, params: Mapping[str, object] | None = None
) -> Strategy:
    classes = registry()
    if implementation not in classes:
        raise KeyError(
            f"unknown strategy implementation {implementation!r}; known: {sorted(classes)}"
        )
    return classes[implementation](strategy_id, params)
