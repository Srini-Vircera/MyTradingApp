"""Strategy catalogue: configuration -> validated strategies + governance records.

* Every configured strategy is built and its parameters *and* its whole
  ``param_grid`` are validated against the implementation's schema. All
  problems are reported together as one :class:`ConfigurationError`.
* Each strategy gets a :class:`StrategyVersion` (id, implementation, code
  version, parameter fingerprint, lifecycle, approval, notes) - the record that
  research results and approvals attach to (Milestones 6 and 8).
* :meth:`StrategyCatalog.eligible` enforces governance by trading mode:
  live mode can only ever use ``live_approved`` strategies, paper/shadow modes
  only strategies promoted to at least ``paper``; research/backtests may use
  anything that is enabled and not disabled.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from adaptive_quant.config.schema import StrategiesConfig, StrategyApproval, StrategyEntry
from adaptive_quant.core.enums import StrategyFamily, StrategyLifecycle, TradingMode
from adaptive_quant.core.errors import ConfigurationError, StrategyError
from adaptive_quant.quant.strategies.base import Strategy
from adaptive_quant.quant.strategies.params import ParamValue
from adaptive_quant.quant.strategies.registry import registry

L = StrategyLifecycle

#: Which lifecycle states may generate signals in each trading mode.
ELIGIBLE_LIFECYCLES: dict[TradingMode, frozenset[StrategyLifecycle]] = {
    TradingMode.BACKTEST: frozenset({L.RESEARCH, L.VALIDATED, L.PAPER, L.SHADOW, L.LIVE_APPROVED}),
    TradingMode.SHADOW: frozenset({L.PAPER, L.SHADOW, L.LIVE_APPROVED}),
    TradingMode.PAPER: frozenset({L.PAPER, L.SHADOW, L.LIVE_APPROVED}),
    TradingMode.LIVE: frozenset({L.LIVE_APPROVED}),
}


@dataclass(frozen=True)
class StrategyVersion:
    strategy_id: str
    implementation: str
    family: StrategyFamily
    code_version: str
    version_id: str
    params: dict[str, ParamValue]
    param_grid: dict[str, list[ParamValue]]
    lifecycle: StrategyLifecycle
    enabled: bool
    warmup_bars: int
    approval: StrategyApproval | None = None
    notes: str = ""


@dataclass(frozen=True)
class CatalogEntry:
    version: StrategyVersion
    strategy: Strategy


@dataclass(frozen=True)
class StrategyCatalog:
    entries: tuple[CatalogEntry, ...] = field(default=())

    @classmethod
    def from_config(cls, config: StrategiesConfig) -> StrategyCatalog:
        errors: list[str] = []
        entries: list[CatalogEntry] = []
        for entry in config.strategies:
            try:
                entries.append(_build(entry))
            except (StrategyError, KeyError, ValueError) as exc:
                errors.append(f"{entry.id}: {exc}")
        if errors:
            raise ConfigurationError(
                "strategy configuration is invalid:\n  - " + "\n  - ".join(errors),
                hint="fix config/strategies.yaml; `aq strategies list` shows valid parameters",
            )
        return cls(tuple(entries))

    def __len__(self) -> int:
        return len(self.entries)

    def get(self, strategy_id: str) -> CatalogEntry:
        for e in self.entries:
            if e.version.strategy_id == strategy_id:
                return e
        raise KeyError(strategy_id)

    def eligible(self, mode: TradingMode) -> list[Strategy]:
        allowed = ELIGIBLE_LIFECYCLES[mode]
        return [
            e.strategy for e in self.entries if e.version.enabled and e.version.lifecycle in allowed
        ]

    @property
    def symbols(self) -> tuple[list[str], list[str]]:
        """(required signal symbols, optional symbols) across all strategies."""
        required = sorted({e.strategy.signal_symbol for e in self.entries})
        optional = sorted(
            {s for e in self.entries for s in e.strategy.optional_symbols} - set(required)
        )
        return required, optional


def _build(entry: StrategyEntry) -> CatalogEntry:
    classes = registry()
    impl = entry.implementation_name
    if impl not in classes:
        raise KeyError(f"unknown implementation {impl!r}")
    cls = classes[impl]
    if cls.family is not entry.family:
        raise ValueError(f"family {entry.family} does not match implementation family {cls.family}")
    strategy = cls(entry.id, entry.params)
    specs = {s.name: s for s in cls.all_param_specs()}
    grid_errors = []
    for name, values in entry.param_grid.items():
        if name not in specs:
            grid_errors.append(f"param_grid: unknown parameter {name!r}")
            continue
        for value in values:
            try:
                specs[name].validate(value)
            except ValueError as exc:
                grid_errors.append(f"param_grid: {exc}")
    if grid_errors:
        raise ValueError("; ".join(grid_errors))
    version = StrategyVersion(
        strategy_id=entry.id,
        implementation=impl,
        family=cls.family,
        code_version=cls.version,
        version_id=strategy.version_id,
        params=dict(strategy.params),
        param_grid={k: list(v) for k, v in entry.param_grid.items()},
        lifecycle=entry.lifecycle,
        enabled=entry.enabled,
        warmup_bars=strategy.warmup_bars,
        approval=entry.approval,
        notes=entry.notes,
    )
    return CatalogEntry(version, strategy)
