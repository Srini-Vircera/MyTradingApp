"""Runtime configuration overlay: editable allowlist, tighten-only risk, never live."""

from __future__ import annotations

from typing import Any

import pytest

from adaptive_quant.config.loader import LoadedConfig, load_config
from adaptive_quant.config.runtime import (
    EDITABLE,
    SECRET_SETTINGS,
    SettingClass,
    apply_runtime,
    classify,
    settings_catalog,
)
from adaptive_quant.core.errors import ConfigurationError, SafetyViolation
from adaptive_quant.services.runtime import resolve
from tests.conftest import REPO_CONFIG


@pytest.fixture(scope="module")
def base() -> LoadedConfig:
    return load_config("development", config_dir=REPO_CONFIG)


@pytest.fixture(scope="module")
def prod() -> LoadedConfig:
    return load_config("production", config_dir=REPO_CONFIG)


def test_empty_overlay_keeps_the_reviewed_config(base: LoadedConfig) -> None:
    assert apply_runtime(base, {}, {}) is base


def test_overlay_changes_value_and_config_version(base: LoadedConfig) -> None:
    eff = apply_runtime(base, {"backtest.initial_capital": 50_000}, revision=3)
    assert eff.settings.backtest.initial_capital == 50_000
    assert eff.config_version != base.config_version
    assert eff.config_version.startswith("development-")
    assert any("revision 3" in w for w in eff.warnings)
    again = apply_runtime(base, {"backtest.initial_capital": 50_000}, revision=4)
    assert again.config_version == eff.config_version  # content-addressed


@pytest.mark.parametrize(
    "path",
    ["trading.live_trading.enabled", "broker.alpaca.paper_base_url", "risk.drawdown_bands",
     "research.gates.min_dsr", "api.cors_origins", "data.staleness.max_age_sessions",
     "app.environment", "trading.kill_switch.backend", "no.such.path"],
)  # fmt: skip
def test_non_editable_settings_are_refused(base: LoadedConfig, path: str) -> None:
    with pytest.raises(ConfigurationError, match="cannot be changed at runtime"):
        apply_runtime(base, {path: 1})


@pytest.mark.parametrize("mode", ["live", "LIVE", "backtest", "", None])
def test_trading_mode_is_shadow_or_paper_only(base: LoadedConfig, mode: Any) -> None:
    with pytest.raises(SafetyViolation):
        apply_runtime(base, {"trading.mode": mode})


def test_paper_mode_is_allowed(prod: LoadedConfig) -> None:
    assert prod.settings.trading.mode.value == "shadow"
    eff = apply_runtime(prod, {"trading.mode": "paper"})
    assert eff.settings.trading.mode.value == "paper"
    assert not eff.settings.trading.mode.uses_real_money


@pytest.mark.parametrize(
    ("path", "looser"),
    [("risk.max_gross_exposure", 1.5), ("risk.max_daily_loss", 0.5),
     ("risk.max_order_notional", 10_000_000), ("risk.min_cash_weight", -0.1),
     ("risk.volatility_target.max_scale", 2.0)],
)  # fmt: skip
def test_risk_limits_can_only_tighten(base: LoadedConfig, path: str, looser: float) -> None:
    with pytest.raises(SafetyViolation, match="loosen"):
        apply_runtime(base, {path: looser})


def test_tightening_is_allowed(base: LoadedConfig) -> None:
    eff = apply_runtime(
        base,
        {"risk.max_daily_loss": 0.03, "risk.max_gross_exposure": 0.9, "risk.min_cash_weight": 0.05},
    )
    assert eff.settings.risk.max_daily_loss == 0.03
    assert eff.settings.risk.min_cash_weight == 0.05


def test_cross_field_risk_rules_still_apply(base: LoadedConfig) -> None:
    with pytest.raises(ConfigurationError, match="min_cash_weight"):
        apply_runtime(base, {"risk.min_cash_weight": 0.05})  # gross 1.0 + 5% cash > 100%


def test_invalid_values_fail_pydantic_validation(base: LoadedConfig) -> None:
    with pytest.raises(ConfigurationError, match="invalid"):
        apply_runtime(base, {"backtest.initial_capital": -5})
    with pytest.raises(ConfigurationError):
        apply_runtime(base, {"backtest.execution": "whenever"})


def test_live_approved_is_never_grantable(base: LoadedConfig) -> None:
    with pytest.raises(SafetyViolation, match="live_approved"):
        apply_runtime(base, {}, {"baseline_buy_hold": {"lifecycle": "live_approved"}})


def test_strategy_overrides_are_structured(base: LoadedConfig) -> None:
    with pytest.raises(ConfigurationError):
        apply_runtime(base, {}, {"baseline_buy_hold": {"implementation": "evil"}})
    with pytest.raises(ConfigurationError, match="unknown"):
        apply_runtime(base, {}, {"nope": {"enabled": False}})


def test_resolve_validates_strategy_params(base: LoadedConfig) -> None:
    sid = next(s.id for s in base.settings.strategies.strategies if s.params)
    with pytest.raises(ConfigurationError):
        resolve(base, {}, {sid: {"params": {"definitely_not_a_param": 1}}}, 1)


def test_catalog_classifies_and_never_shows_secrets(base: LoadedConfig) -> None:
    rows = settings_catalog(base.settings, base.settings)
    by = {r["path"]: r for r in rows}
    for spec in EDITABLE:
        assert by[spec.path]["class"] == spec.klass.value
    assert by["trading.live_trading.enabled"]["class"] == SettingClass.IMMUTABLE.value
    assert classify("broker.alpaca.live_base_url")[1] is SettingClass.IMMUTABLE
    text = repr(rows).lower()
    for name, _cat, _label in SECRET_SETTINGS:
        assert name.lower() not in text
    assert "postgresql://" not in text
