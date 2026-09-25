"""Build the configured broker adapter (paper only in this version)."""

from __future__ import annotations

from adaptive_quant.config.loader import LoadedConfig
from adaptive_quant.config.secrets import Secrets
from adaptive_quant.core.clock import Clock
from adaptive_quant.core.errors import ConfigurationError, SafetyViolation
from adaptive_quant.trading.brokers.alpaca import AlpacaPaperBroker
from adaptive_quant.trading.brokers.base import BrokerAdapter, assert_broker_matches_mode


def build_broker(loaded: LoadedConfig, secrets: Secrets, clock: Clock) -> BrokerAdapter:
    s = loaded.settings
    if s.trading.mode.uses_real_money:
        raise SafetyViolation(
            "no live broker adapter is enabled in this version",
            hint="paper and shadow modes only; see docs/SAFETY.md",
        )
    if s.broker.provider != "alpaca":
        raise ConfigurationError(
            f"broker.provider {s.broker.provider!r} cannot be built from configuration",
            hint="the simulated broker is for tests and drills and is constructed in code",
        )
    secrets.require("alpaca_api_key_id", "alpaca_api_secret_key")
    broker = AlpacaPaperBroker(
        key_id=secrets.secret("alpaca_api_key_id"),
        secret_key=secrets.secret("alpaca_api_secret_key"),
        base_url=s.broker.alpaca.paper_base_url,
        timeout_seconds=s.broker.alpaca.request_timeout_seconds,
        clock=clock,
    )
    assert_broker_matches_mode(broker, s.trading.mode)
    return broker
