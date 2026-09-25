"""Enumerations shared across the platform.

All enums are ``StrEnum`` so they serialise to readable strings in logs,
YAML and the database.
"""

from __future__ import annotations

from enum import StrEnum


class DeploymentEnvironment(StrEnum):
    """Which configuration overlay is active (``config/<name>.yaml``)."""

    DEVELOPMENT = "development"
    PAPER = "paper"
    PRODUCTION = "production"


class TradingMode(StrEnum):
    """How (and whether) the system interacts with a broker.

    Ordered from safest to most dangerous.
    """

    BACKTEST = "backtest"
    """Historical simulation only. No broker connection."""

    SHADOW = "shadow"
    """Reads a real broker account, computes and records orders, submits nothing."""

    PAPER = "paper"
    """Submits orders to a broker *paper* (simulated) account."""

    LIVE = "live"
    """Submits orders with real money. Requires explicit multi-factor opt-in."""

    @property
    def submits_orders(self) -> bool:
        """True if this mode transmits orders to a broker."""
        return self in (TradingMode.PAPER, TradingMode.LIVE)

    @property
    def uses_real_money(self) -> bool:
        """True only for :attr:`LIVE`."""
        return self is TradingMode.LIVE


class AssetClass(StrEnum):
    """Asset classes. Only equities/ETFs are tradeable in version 1."""

    EQUITY = "equity"
    ETF = "etf"
    INDEX = "index"  # signal-only (e.g. NDX); never tradeable
    CASH = "cash"
    OPTION = "option"  # reserved for a future module; rejected by v1 config
    FUTURE = "future"  # reserved for a future module; rejected by v1 config


class OrderSide(StrEnum):
    BUY = "buy"
    SELL = "sell"


class OrderType(StrEnum):
    MARKET = "market"
    LIMIT = "limit"
    MARKET_ON_CLOSE = "market_on_close"
    LIMIT_ON_CLOSE = "limit_on_close"


class TimeInForce(StrEnum):
    DAY = "day"
    CLS = "cls"  # closing auction
    IOC = "ioc"
    GTC = "gtc"


class OrderState(StrEnum):
    """Internal order lifecycle. See ``trading/orders/state_machine.py``."""

    CREATED = "created"
    VALIDATED = "validated"
    SUBMITTED = "submitted"
    ACKNOWLEDGED = "acknowledged"
    PARTIALLY_FILLED = "partially_filled"
    FILLED = "filled"
    CANCELLED = "cancelled"
    REJECTED = "rejected"
    UNKNOWN = "unknown"


class SignalDirection(StrEnum):
    BULLISH = "bullish"
    NEUTRAL = "neutral"
    BEARISH = "bearish"


class StrategyFamily(StrEnum):
    LONG_TERM_TREND = "long_term_trend"
    INTERMEDIATE_TREND = "intermediate_trend"
    SHORT_TERM_TREND = "short_term_trend"
    MOMENTUM = "momentum"
    MOMENTUM_ACCELERATION = "momentum_acceleration"
    BREAKOUT = "breakout"
    MEAN_REVERSION_LONG = "mean_reversion_long"
    DEFENSIVE_MEAN_REVERSION = "defensive_mean_reversion"
    BEARISH_MOMENTUM = "bearish_momentum"
    VOLATILITY_REGIME = "volatility_regime"
    TREND_PLUS_VOLATILITY = "trend_plus_volatility"
    BREAKOUT_PLUS_MOMENTUM = "breakout_plus_momentum"
    MARKET_EXTENSION = "market_extension"
    DRAWDOWN_AWARE = "drawdown_aware"
    REGIME_TRANSITION = "regime_transition"
    BENCHMARK = "benchmark"  # baselines every candidate must beat (buy & hold, cash)


class StrategyLifecycle(StrEnum):
    """Model-governance states. See ``governance/lifecycle.py``."""

    RESEARCH = "research"
    VALIDATED = "validated"
    PAPER = "paper"
    SHADOW = "shadow"
    LIVE_APPROVED = "live_approved"
    DISABLED = "disabled"


class EnsembleMethod(StrEnum):
    EQUAL = "equal"
    FIXED = "fixed"
    RISK_ADJUSTED = "risk_adjusted"
    WALK_FORWARD = "walk_forward"


class Severity(StrEnum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"
    CRITICAL = "critical"

    @property
    def rank(self) -> int:
        return _SEVERITY_RANK[self]


_SEVERITY_RANK = {
    Severity.INFO: 0,
    Severity.WARNING: 1,
    Severity.ERROR: 2,
    Severity.CRITICAL: 3,
}
