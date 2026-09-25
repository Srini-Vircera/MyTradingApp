"""What the API is allowed to touch: read-only audit queries, configuration
(read-only) and the kill switch. No broker, no order manager."""

from __future__ import annotations

from dataclasses import dataclass

from pydantic import SecretStr

from adaptive_quant.config.loader import LoadedConfig
from adaptive_quant.core.clock import Clock
from adaptive_quant.core.errors import ConfigurationError
from adaptive_quant.persistence.db import Database
from adaptive_quant.persistence.reads import AuditReads
from adaptive_quant.quant.data.calendar import TradingCalendar
from adaptive_quant.trading.safety.kill_switch import KillSwitch


@dataclass
class ApiServices:
    loaded: LoadedConfig
    token: SecretStr
    kill_switch: KillSwitch
    clock: Clock
    calendar: TradingCalendar
    db: Database | None = None

    def __post_init__(self) -> None:
        n = self.loaded.settings.api.min_bearer_chars
        if len(self.token.get_secret_value()) < n:
            raise ConfigurationError(
                f"AQ_API_TOKEN must be at least {n} characters",
                hint="python -c 'import secrets; print(secrets.token_urlsafe(48))'",
            )

    @property
    def reads(self) -> AuditReads | None:
        return None if self.db is None else AuditReads(self.db)
