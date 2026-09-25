"""Build data components from configuration (the only place providers are chosen)."""

from __future__ import annotations

import httpx

from adaptive_quant.config.loader import LoadedConfig
from adaptive_quant.config.secrets import Secrets
from adaptive_quant.core.clock import Clock
from adaptive_quant.core.errors import ConfigurationError
from adaptive_quant.quant.data.bars import Adjustment
from adaptive_quant.quant.data.calendar import TradingCalendar
from adaptive_quant.quant.data.providers.alpaca import AlpacaDataProvider
from adaptive_quant.quant.data.providers.base import MarketDataProvider
from adaptive_quant.quant.data.providers.files import FileDataProvider
from adaptive_quant.quant.data.providers.polygon import PolygonDataProvider
from adaptive_quant.quant.data.store import ParquetBarStore
from adaptive_quant.quant.data.validation import BarValidator, ValidationPolicy

PROVIDERS = ("file", "alpaca", "polygon")


def build_provider(
    name: str,
    loaded: LoadedConfig,
    secrets: Secrets,
    calendar: TradingCalendar,
    *,
    transport: httpx.BaseTransport | None = None,
) -> MarketDataProvider:
    data = loaded.settings.data
    retry = data.http
    if name == "file":
        return FileDataProvider(
            loaded.resolve_path(data.file_import.directory),
            Adjustment(data.file_import.adjustment),
            calendar,
        )
    if name == "alpaca":
        secrets.require("alpaca_api_key_id", "alpaca_api_secret_key")
        return AlpacaDataProvider(
            key_id=secrets.secret("alpaca_api_key_id"),
            secret_key=secrets.secret("alpaca_api_secret_key"),
            config=loaded.settings.broker.alpaca,
            calendar=calendar,
            max_retries=retry.max_retries,
            backoff_seconds=retry.backoff_seconds,
            transport=transport,
        )
    if name == "polygon":
        return PolygonDataProvider(
            api_key=secrets.secret("polygon_api_key"),
            base_url=data.polygon.base_url,
            timeout_seconds=data.polygon.request_timeout_seconds,
            calendar=calendar,
            symbol_map=data.polygon.symbol_map,
            max_retries=retry.max_retries,
            backoff_seconds=retry.backoff_seconds,
            transport=transport,
        )
    raise ConfigurationError(f"unknown data provider {name!r}", hint=f"choose one of {PROVIDERS}")


def build_store(loaded: LoadedConfig, clock: Clock) -> ParquetBarStore:
    return ParquetBarStore(loaded.resolve_path(loaded.settings.data.root_dir), clock)


def build_validator(loaded: LoadedConfig, calendar: TradingCalendar) -> BarValidator:
    return BarValidator(
        calendar, ValidationPolicy(max_abs_return=loaded.settings.data.max_abs_daily_return)
    )
