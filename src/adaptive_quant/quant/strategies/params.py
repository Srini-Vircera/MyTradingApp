"""Typed, bounded strategy parameters."""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from types import MappingProxyType

ParamValue = int | float | str | bool


@dataclass(frozen=True)
class ParamSpec:
    name: str
    kind: type[int] | type[float] | type[str] | type[bool]
    default: ParamValue
    minimum: float | None = None
    maximum: float | None = None
    choices: tuple[ParamValue, ...] | None = None
    description: str = ""

    def validate(self, value: object) -> ParamValue:
        """Return the value coerced to this parameter's type, or raise ``ValueError``."""
        name = self.name
        if self.kind is bool:
            if not isinstance(value, bool):
                raise ValueError(f"{name} must be true/false, got {value!r}")
            out: ParamValue = value
        elif self.kind is int:
            if isinstance(value, bool) or not isinstance(value, int):
                raise ValueError(f"{name} must be an integer, got {value!r}")
            out = value
        elif self.kind is float:
            if isinstance(value, bool) or not isinstance(value, int | float):
                raise ValueError(f"{name} must be a number, got {value!r}")
            if not math.isfinite(float(value)):
                raise ValueError(f"{name} must be finite")
            out = float(value)
        else:
            if not isinstance(value, str):
                raise ValueError(f"{name} must be text, got {value!r}")
            out = value
        if isinstance(out, int | float) and not isinstance(out, bool):
            if self.minimum is not None and out < self.minimum:
                raise ValueError(f"{name}={out} is below the minimum {self.minimum}")
            if self.maximum is not None and out > self.maximum:
                raise ValueError(f"{name}={out} is above the maximum {self.maximum}")
        if self.choices is not None and out not in self.choices:
            raise ValueError(f"{name} must be one of {list(self.choices)}, got {out!r}")
        return out


def resolve_params(
    specs: Iterable[ParamSpec], given: Mapping[str, object] | None
) -> Mapping[str, ParamValue]:
    """Validate ``given`` against ``specs`` and fill defaults. Collects all errors."""
    by_name = {s.name: s for s in specs}
    given = dict(given or {})
    errors = [f"unknown parameter {k!r}" for k in given if k not in by_name]
    resolved: dict[str, ParamValue] = {}
    for name, spec in by_name.items():
        try:
            resolved[name] = (
                spec.validate(given[name]) if name in given else spec.validate(spec.default)
            )
        except ValueError as exc:
            errors.append(str(exc))
    if errors:
        raise ValueError("; ".join(errors))
    return MappingProxyType(resolved)


def parse_int_list(text: str, name: str, minimum: int = 1, maximum: int = 1000) -> tuple[int, ...]:
    """Parse ``"5,10,20"`` into a sorted tuple of unique ints within bounds."""
    try:
        values = tuple(sorted({int(p) for p in text.split(",") if p.strip()}))
    except ValueError as exc:
        raise ValueError(f"{name} must be a comma-separated list of integers") from exc
    if not values:
        raise ValueError(f"{name} must not be empty")
    if values[0] < minimum or values[-1] > maximum:
        raise ValueError(f"{name} values must lie in [{minimum}, {maximum}]")
    return values
