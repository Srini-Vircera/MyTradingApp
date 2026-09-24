"""Typed configuration schema.

Every section rejects unknown keys (``extra="forbid"``) so a typo such as
``max_gros_exposure`` fails at startup instead of silently using a default.

Numbers in the shipped YAML files are *examples to be researched*, not claims
of optimality. Cross-field rules (e.g. drawdown bands must tighten as
drawdown deepens) are validated here so an inconsistent risk configuration can
never be loaded.
"""

from __future__ import annotations

import math
import re
from datetime import date
from itertools import pairwise
from pathlib import Path
from typing import Annotated, Any, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from adaptive_quant.core.enums import (
    AssetClass,
    DeploymentEnvironment,
    EnsembleMethod,
    Severity,
    StrategyFamily,
    StrategyLifecycle,
    TradingMode,
)
from adaptive_quant.core.models import Instrument
from adaptive_quant.governance.lifecycle import MIN_JUSTIFICATION_CHARS

#: The exact sentence an operator must type into ``trading.live_trading.acknowledgement``.
LIVE_TRADING_ACKNOWLEDGEMENT = "I understand that live mode trades real money"

Fraction = Annotated[float, Field(ge=0.0, le=1.0)]
PositiveFloat = Annotated[float, Field(gt=0.0)]


class Section(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", validate_default=True)


# ============================================================ app / logging
class AppConfig(Section):
    name: str = "adaptive-quant"
    environment: DeploymentEnvironment
    state_dir: Path = Path("var/state")


class LoggingConfig(Section):
    level: str = "INFO"
    format: str = Field(default="json", pattern="^(json|console)$")
    directory: Path = Path("var/log")

    @field_validator("level")
    @classmethod
    def _level(cls, v: str) -> str:
        v = v.upper()
        if v not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
            raise ValueError(f"unknown log level {v!r}")
        return v


# ============================================================ trading mode / safety
class LiveTradingConfig(Section):
    """Live trading is opt-in through several independent switches.

    See :func:`adaptive_quant.config.loader.enforce_trading_mode_policy`.
    """

    enabled: bool = False
    acknowledgement: str = ""


class KillSwitchConfig(Section):
    state_file: Path = Path("var/state/kill_switch.json")
    audit_file: Path = Path("var/state/kill_switch_audit.jsonl")
    allow_risk_reducing_orders: bool = True


class TradingConfig(Section):
    mode: TradingMode = TradingMode.SHADOW
    live_trading: LiveTradingConfig = LiveTradingConfig()
    kill_switch: KillSwitchConfig = KillSwitchConfig()
    allow_fractional_shares: bool = True
    rebalance_threshold_weight: Fraction = 0.02
    max_order_retries_on_confirmed_reject: int = Field(default=0, ge=0, le=3)


# ============================================================ universe
class UniverseConfig(Section):
    signal_symbols: list[str] = Field(min_length=1)
    instruments: list[Instrument] = Field(min_length=1)
    benchmarks: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _check(self) -> Self:
        symbols = [i.symbol for i in self.instruments]
        dupes = {s for s in symbols if symbols.count(s) > 1}
        if dupes:
            raise ValueError(f"duplicate instruments: {sorted(dupes)}")
        known = set(symbols)
        for s in self.signal_symbols:
            if s not in known:
                raise ValueError(f"signal symbol {s!r} is not declared in instruments")
        for inst in self.instruments:
            if inst.underlying is not None and inst.underlying not in known:
                raise ValueError(f"{inst.symbol}: underlying {inst.underlying!r} not declared")
        if not any(i.tradeable for i in self.instruments):
            raise ValueError("universe has no tradeable instruments")
        return self

    @property
    def by_symbol(self) -> dict[str, Instrument]:
        return {i.symbol: i for i in self.instruments}

    @property
    def tradeable_symbols(self) -> list[str]:
        return [i.symbol for i in self.instruments if i.tradeable]


# ============================================================ data
class StalenessConfig(Section):
    max_daily_bar_age_sessions: int = Field(default=1, ge=0)
    max_intraday_bar_age_seconds: int = Field(default=180, gt=0)


class FileImportConfig(Section):
    """Local CSV/Parquet datasets (``aq data download --provider file``)."""

    directory: Path = Path("var/data/import")
    adjustment: str = Field(default="raw", pattern="^(raw|split|all)$")


class PolygonConfig(Section):
    base_url: str = "https://api.polygon.io"
    request_timeout_seconds: PositiveFloat = 15.0
    symbol_map: dict[str, str] = Field(default_factory=dict)


class HttpRetryConfig(Section):
    max_retries: int = Field(default=3, ge=0, le=10)
    backoff_seconds: Annotated[float, Field(ge=0.0, le=60.0)] = 1.0


class RiskFreeConfig(Section):
    """Annual risk-free rate for synthetic financing costs.

    ``csv_path`` (e.g. FRED DTB3, percent) is preferred; the constant is a
    fallback and is recorded as an assumption on every synthetic snapshot.
    """

    constant_annual_rate: Annotated[float, Field(ge=-0.05, le=0.25)] = 0.02
    csv_path: Path | None = None


class SyntheticProductConfig(Section):
    expense_ratio: Annotated[float, Field(ge=0.0, le=0.05)]
    inception: date


class SyntheticConfig(Section):
    """Assumptions for synthetic leveraged-ETF history (see docs/DATA.md)."""

    underlying_symbol: str = "QQQ"
    underlying_expense_addback: Annotated[float, Field(ge=0.0, le=0.02)] = 0.0020
    swap_spread_annual: Annotated[float, Field(ge=0.0, le=0.05)] = 0.0
    risk_free: RiskFreeConfig = RiskFreeConfig()
    max_tracking_error_annual: Annotated[float, Field(gt=0.0, le=0.5)] = 0.03
    products: dict[str, SyntheticProductConfig] = Field(default_factory=dict)


class DataConfig(Section):
    root_dir: Path = Path("var/data")
    primary_provider: str = Field(default="file", pattern="^(file|alpaca|polygon)$")
    history_start: date = date(1999, 3, 10)  # QQQ's first trading day
    staleness: StalenessConfig = StalenessConfig()
    max_abs_daily_return: PositiveFloat = Field(
        default=0.5,
        description="Bars whose close-to-close move exceeds this are flagged as suspect.",
    )
    http: HttpRetryConfig = HttpRetryConfig()
    file_import: FileImportConfig = FileImportConfig()
    polygon: PolygonConfig = PolygonConfig()
    synthetic: SyntheticConfig = SyntheticConfig()


# ============================================================ schedule
class ScheduleStep(Section):
    """A step scheduled relative to the session close.

    Expressing times as *minutes before close* (instead of 15:40 wall-clock)
    makes early-close days (13:00 ET) work automatically.
    """

    name: str = Field(pattern=r"^[a-z_]+$")
    minutes_before_close: int = Field(ge=-120, le=390)


class ScheduleConfig(Section):
    timezone: str = "America/New_York"
    steps: list[ScheduleStep] = Field(min_length=1)
    no_new_orders_after_minutes_before_close: int = Field(default=5, ge=0)

    @model_validator(mode="after")
    def _check(self) -> Self:
        names = [s.name for s in self.steps]
        if len(set(names)) != len(names):
            raise ValueError("schedule step names must be unique")
        offsets = [s.minutes_before_close for s in self.steps]
        if offsets != sorted(offsets, reverse=True):
            raise ValueError("schedule steps must be listed in chronological order")
        return self

    @property
    def order_cutoff(self) -> int:
        return self.no_new_orders_after_minutes_before_close


# ============================================================ broker
class AlpacaConfig(Section):
    paper_base_url: str = "https://paper-api.alpaca.markets"
    live_base_url: str = "https://api.alpaca.markets"
    data_base_url: str = "https://data.alpaca.markets"
    data_feed: str = Field(default="iex", pattern="^(iex|sip)$")
    request_timeout_seconds: PositiveFloat = 10.0


class BrokerConfig(Section):
    provider: str = Field(default="alpaca", pattern="^(alpaca|simulated)$")
    alpaca: AlpacaConfig = AlpacaConfig()


# ============================================================ database
class DatabaseConfig(Section):
    """Connection *settings*. The URL (with password) comes from ``DATABASE_URL``."""

    pool_size: int = Field(default=5, ge=1, le=50)
    statement_timeout_ms: int = Field(default=15_000, gt=0)
    echo_sql: bool = False


# ============================================================ notifications
class EmailConfig(Section):
    enabled: bool = False
    smtp_host: str = ""
    smtp_port: int = Field(default=587, gt=0, lt=65536)
    use_starttls: bool = True
    from_address: str = ""
    to_addresses: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _check(self) -> Self:
        if self.enabled:
            missing = [k for k in ("smtp_host", "from_address") if not getattr(self, k)] + (
                ["to_addresses"] if not self.to_addresses else []
            )
            if missing:
                raise ValueError(f"email is enabled but missing: {missing}")
        return self


class NotificationsConfig(Section):
    min_severity: Severity = Severity.WARNING
    email: EmailConfig = EmailConfig()


# ============================================================ risk
class DrawdownBand(Section):
    """Exposure cap that applies once drawdown from peak equity reaches ``threshold``."""

    name: str = Field(pattern=r"^[a-z_]+$")
    threshold: Fraction
    max_net_exposure: Annotated[float, Field(ge=0.0)]
    block_risk_increasing: bool = False


class VolatilityTargetConfig(Section):
    enabled: bool = True
    annualized_target: Annotated[float, Field(gt=0.0, le=2.0)] = 0.20
    lookback_days: int = Field(default=20, ge=5, le=504)
    min_scale: Annotated[float, Field(ge=0.0)] = 0.0
    max_scale: Annotated[float, Field(gt=0.0)] = 1.0

    @model_validator(mode="after")
    def _check(self) -> Self:
        if self.min_scale > self.max_scale:
            raise ValueError("volatility_target.min_scale must be <= max_scale")
        return self


class VolatilityRegimeConfig(Section):
    """Percentile boundaries (of trailing realised volatility) between regimes."""

    lookback_days: int = Field(default=252, ge=60)
    low_below_pct: Fraction = 0.25
    elevated_above_pct: Fraction = 0.75
    extreme_above_pct: Fraction = 0.95

    @model_validator(mode="after")
    def _check(self) -> Self:
        if not self.low_below_pct < self.elevated_above_pct < self.extreme_above_pct:
            raise ValueError("volatility regime percentiles must be strictly increasing")
        return self


class RiskConfig(Section):
    max_gross_exposure: Fraction = 1.0
    max_net_underlying_exposure: Annotated[float, Field(ge=0.0, le=3.0)]
    max_inverse_net_exposure: Annotated[float, Field(ge=0.0, le=3.0)]
    max_leveraged_etf_weight: Fraction
    max_position_weight: dict[str, Fraction] = Field(default_factory=dict)
    default_max_position_weight: Fraction = 1.0
    max_daily_turnover: Annotated[float, Field(ge=0.0, le=4.0)]
    max_daily_loss: Fraction
    max_order_notional: PositiveFloat
    max_portfolio_drift: Fraction
    min_cash_weight: Fraction = 0.0
    volatility_target: VolatilityTargetConfig = VolatilityTargetConfig()
    volatility_regimes: VolatilityRegimeConfig = VolatilityRegimeConfig()
    drawdown_bands: list[DrawdownBand] = Field(min_length=1)

    @model_validator(mode="after")
    def _check(self) -> Self:
        bands = self.drawdown_bands
        if bands[0].threshold != 0.0:
            raise ValueError("the first drawdown band must start at threshold 0.0")
        for prev, cur in pairwise(bands):
            if cur.threshold <= prev.threshold:
                raise ValueError("drawdown band thresholds must be strictly increasing")
            if cur.max_net_exposure > prev.max_net_exposure:
                raise ValueError(
                    f"drawdown band {cur.name!r} allows more exposure than {prev.name!r}; "
                    "exposure caps must not loosen as drawdown deepens"
                )
            if prev.block_risk_increasing and not cur.block_risk_increasing:
                raise ValueError("once a band blocks risk-increasing orders, deeper ones must too")
        if not bands[-1].block_risk_increasing:
            raise ValueError("the deepest drawdown band must block risk-increasing orders")
        if len({b.name for b in bands}) != len(bands):
            raise ValueError("drawdown band names must be unique")
        if self.max_gross_exposure + self.min_cash_weight > 1.0 + 1e-9:
            raise ValueError("max_gross_exposure + min_cash_weight must not exceed 1.0")
        return self

    def position_cap(self, symbol: str) -> float:
        return self.max_position_weight.get(symbol, self.default_max_position_weight)


# ============================================================ strategies / ensemble
ParamValue = int | float | str | bool


class StrategyApproval(Section):
    """Written human approval; required to declare a strategy ``live_approved``."""

    approved_by: str = Field(min_length=1)
    approved_on: date
    justification: str

    @field_validator("justification")
    @classmethod
    def _justify(cls, v: str) -> str:
        if len(v.strip()) < MIN_JUSTIFICATION_CHARS:
            raise ValueError(
                f"approval justification must be at least {MIN_JUSTIFICATION_CHARS} characters"
            )
        return v.strip()


class StrategyEntry(Section):
    id: str = Field(pattern=r"^[a-z0-9_]+$")
    implementation: str | None = Field(
        default=None,
        pattern=r"^[a-z0-9_]+$",
        description="Registered strategy class; defaults to the id.",
    )
    family: StrategyFamily
    enabled: bool = True
    lifecycle: StrategyLifecycle = StrategyLifecycle.RESEARCH
    approval: StrategyApproval | None = None
    params: dict[str, ParamValue] = Field(default_factory=dict)
    param_grid: dict[str, list[ParamValue]] = Field(default_factory=dict)
    notes: str = ""

    @property
    def implementation_name(self) -> str:
        return self.implementation or self.id

    @model_validator(mode="after")
    def _check(self) -> Self:
        if self.lifecycle is StrategyLifecycle.LIVE_APPROVED and self.approval is None:
            raise ValueError(
                f"{self.id}: lifecycle live_approved requires an 'approval' block "
                "(approved_by, approved_on, justification) written by a human"
            )
        for key, values in self.param_grid.items():
            if not values:
                raise ValueError(f"{self.id}: param_grid[{key!r}] is empty")
        for key, val in self.params.items():
            if isinstance(val, float) and not math.isfinite(val):
                raise ValueError(f"{self.id}: parameter {key!r} is not finite")
        return self


class EnsembleConfig(Section):
    method: EnsembleMethod = EnsembleMethod.EQUAL
    fixed_weights: dict[str, Annotated[float, Field(ge=0.0)]] = Field(default_factory=dict)
    max_family_weight: Fraction = 0.4
    max_pairwise_correlation: Annotated[float, Field(ge=-1.0, le=1.0)] = 0.85


class StrategiesConfig(Section):
    ensemble: EnsembleConfig = EnsembleConfig()
    strategies: list[StrategyEntry] = Field(default_factory=list)

    @model_validator(mode="after")
    def _check(self) -> Self:
        ids = [s.id for s in self.strategies]
        dupes = {i for i in ids if ids.count(i) > 1}
        if dupes:
            raise ValueError(f"duplicate strategy ids: {sorted(dupes)}")
        if self.ensemble.method is EnsembleMethod.FIXED:
            unknown = set(self.ensemble.fixed_weights) - set(ids)
            if unknown:
                raise ValueError(f"fixed_weights reference unknown strategies: {sorted(unknown)}")
            if not self.ensemble.fixed_weights or sum(self.ensemble.fixed_weights.values()) <= 0:
                raise ValueError("fixed ensemble requires positive fixed_weights")
        return self


# ============================================================ root
class Settings(Section):
    """The fully resolved, validated application configuration."""

    app: AppConfig
    logging: LoggingConfig = LoggingConfig()
    trading: TradingConfig = TradingConfig()
    universe: UniverseConfig
    data: DataConfig = DataConfig()
    schedule: ScheduleConfig
    broker: BrokerConfig = BrokerConfig()
    database: DatabaseConfig = DatabaseConfig()
    notifications: NotificationsConfig = NotificationsConfig()
    risk: RiskConfig
    strategies: StrategiesConfig = StrategiesConfig()

    @model_validator(mode="after")
    def _check(self) -> Self:
        caps = self.risk.max_position_weight
        unknown = set(caps) - set(self.universe.tradeable_symbols)
        if unknown:
            raise ValueError(f"risk.max_position_weight names unknown symbols: {sorted(unknown)}")
        for symbol in self.universe.benchmarks:
            if not re.fullmatch(r"^[A-Z0-9.^-]+$", symbol):
                raise ValueError(f"invalid benchmark symbol {symbol!r}")
        if any(i.asset_class is AssetClass.CASH for i in self.universe.instruments):
            raise ValueError("cash is implicit; do not declare it as an instrument")
        self._check_synthetic()
        return self

    def _check_synthetic(self) -> None:
        syn = self.data.synthetic
        instruments = self.universe.by_symbol
        if syn.underlying_symbol not in instruments:
            raise ValueError(
                f"data.synthetic.underlying_symbol {syn.underlying_symbol!r} is not an instrument"
            )
        for symbol, product in syn.products.items():
            inst = instruments.get(symbol)
            if inst is None or not inst.is_leveraged:
                raise ValueError(
                    f"data.synthetic.products: {symbol!r} is not a leveraged instrument"
                )
            if inst.underlying != syn.underlying_symbol:
                raise ValueError(
                    f"data.synthetic.products: {symbol} tracks {inst.underlying}, "
                    f"not {syn.underlying_symbol}"
                )
            if product.inception <= self.data.history_start:
                raise ValueError(f"{symbol}: inception must be after data.history_start")


def redact(data: Any) -> Any:
    """Recursively redact secret-looking keys (defence in depth for dumps and logs)."""
    if isinstance(data, dict):
        return {
            k: ("***" if is_secret_key(str(k)) and v not in (None, "") else redact(v))
            for k, v in data.items()
        }
    if isinstance(data, list):
        return [redact(v) for v in data]
    return data


_SECRET_KEY = re.compile(
    r"(pass(word|wd)?|secret|api[_-]?key|token|private[_-]?key|credential|key[_-]?id)",
    re.IGNORECASE,
)


def is_secret_key(key: str) -> bool:
    return bool(_SECRET_KEY.search(key))
