"""Exception hierarchy.

Every error carries a human-readable ``hint`` telling the operator what to do,
because the operator is not necessarily a software engineer. Catch the most
specific class you can handle; never catch :class:`AQError` just to ignore it.
"""

from __future__ import annotations


class AQError(Exception):
    """Base class for all platform errors."""

    def __init__(self, message: str, *, hint: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.hint = hint

    def __str__(self) -> str:
        if self.hint:
            return f"{self.message}\n  -> What to do: {self.hint}"
        return self.message


class ConfigurationError(AQError):
    """Configuration is missing, malformed, or unsafe."""


class MissingSecretError(ConfigurationError):
    """A required credential is not present in the environment."""


class SafetyViolation(AQError):
    """An action would violate a safety invariant (e.g. live trading not authorised)."""


class TradingRefused(SafetyViolation):
    """The pre-trade safety gate refused to trade this cycle."""


class DataQualityError(AQError):
    """Market data failed validation."""


class StaleDataError(DataQualityError):
    """Market data is older than the configured tolerance."""


class MissingDataError(DataQualityError):
    """Required market data is absent."""


class DataProviderError(AQError):
    """A market-data provider could not be reached or returned unusable data."""


class ProviderAuthError(DataProviderError):
    """The provider rejected our credentials."""


class CorporateActionsUnavailable(DataProviderError):
    """The provider cannot supply corporate actions for this request."""


class CalendarError(AQError):
    """A trading-calendar query fell outside the calendar's coverage."""


class BrokerError(AQError):
    """The broker could not be reached or returned an unexpected response."""


class OrderStateError(AQError):
    """An order state transition is not allowed."""


class ReconciliationError(AQError):
    """Broker state and internal state disagree beyond tolerance."""


class RiskError(AQError):
    """Risk calculation failed. Treated as a refusal to trade (fail closed)."""


class StrategyError(AQError):
    """A strategy could not produce a valid signal. Treated as a refusal to trade."""


class InsufficientHistoryError(StrategyError):
    """Not enough point-in-time history for the strategy's warm-up."""


class GovernanceError(AQError):
    """A strategy lifecycle transition is not permitted."""


class PersistenceError(AQError):
    """The audit database rejected or could not complete an operation."""


class DatabaseUnavailableError(PersistenceError):
    """The audit database cannot be reached: nothing that must be recorded may proceed."""
