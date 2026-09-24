from pathlib import Path
from typing import Any

import pytest
from pydantic import SecretStr

from adaptive_quant.config.loader import (
    LIVE_CONFIRM_ENV_VALUE,
    deep_merge,
    load_config,
)
from adaptive_quant.config.schema import LIVE_TRADING_ACKNOWLEDGEMENT
from adaptive_quant.config.secrets import Secrets
from adaptive_quant.core.enums import DeploymentEnvironment, TradingMode
from adaptive_quant.core.errors import ConfigurationError, SafetyViolation
from tests.conftest import REPO_CONFIG, PatchYaml


class TestShippedConfiguration:
    @pytest.mark.parametrize("env", list(DeploymentEnvironment))
    def test_every_environment_loads(self, env: DeploymentEnvironment) -> None:
        loaded = load_config(env, config_dir=REPO_CONFIG)
        assert loaded.settings.app.environment is env
        assert loaded.config_version.startswith(env.value)

    @pytest.mark.parametrize("env", list(DeploymentEnvironment))
    def test_no_shipped_environment_trades_real_money(self, env: DeploymentEnvironment) -> None:
        settings = load_config(env, config_dir=REPO_CONFIG).settings
        assert settings.trading.mode is not TradingMode.LIVE
        assert settings.trading.live_trading.enabled is False

    def test_paper_environment_is_paper(self) -> None:
        assert (
            load_config("paper", config_dir=REPO_CONFIG).settings.trading.mode is TradingMode.PAPER
        )

    def test_env_var_selects_environment(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("AQ_ENV", "paper")
        assert load_config(config_dir=REPO_CONFIG).settings.app.environment.value == "paper"

    def test_defaults_to_development(self) -> None:
        assert load_config(config_dir=REPO_CONFIG).settings.app.environment.value == "development"


class TestValidationErrors:
    def test_unknown_environment(self) -> None:
        with pytest.raises(ConfigurationError, match="unknown environment"):
            load_config("staging", config_dir=REPO_CONFIG)

    def test_missing_directory(self, tmp_path: Path) -> None:
        with pytest.raises(ConfigurationError, match="not found"):
            load_config("development", config_dir=tmp_path / "nope")

    def test_typo_in_key_is_rejected(self, config_dir: Path, patch_yaml: PatchYaml) -> None:
        patch_yaml("risk.yaml", lambda d: d["risk"].update({"max_gros_exposure": 0.5}))
        with pytest.raises(ConfigurationError, match="max_gros_exposure"):
            load_config("development", config_dir=config_dir)

    def test_invalid_yaml(self, config_dir: Path) -> None:
        (config_dir / "risk.yaml").write_text("risk: [unclosed")
        with pytest.raises(ConfigurationError, match="not valid YAML"):
            load_config("development", config_dir=config_dir)

    def test_missing_file(self, config_dir: Path) -> None:
        (config_dir / "strategies.yaml").unlink()
        with pytest.raises(ConfigurationError, match="missing configuration file"):
            load_config("development", config_dir=config_dir)

    def test_mismatched_environment_declaration(
        self, config_dir: Path, patch_yaml: PatchYaml
    ) -> None:
        patch_yaml("paper.yaml", lambda d: d["app"].update({"environment": "production"}))
        with pytest.raises(ConfigurationError, match=r"declares app\.environment"):
            load_config("paper", config_dir=config_dir)

    @pytest.mark.parametrize(
        ("key", "value"),
        [("api_key", "PKABC123"), ("password", "hunter2"), ("ALPACA_SECRET", "x")],
    )
    def test_secrets_in_yaml_rejected(
        self, config_dir: Path, patch_yaml: PatchYaml, key: str, value: str
    ) -> None:
        patch_yaml("base.yaml", lambda d: d["broker"]["alpaca"].update({key: value}))
        with pytest.raises(ConfigurationError, match="looks like a credential"):
            load_config("development", config_dir=config_dir)

    def test_error_message_is_readable(self, config_dir: Path, patch_yaml: PatchYaml) -> None:
        patch_yaml("risk.yaml", lambda d: d["risk"].update({"max_daily_loss": 1.5}))
        with pytest.raises(ConfigurationError) as info:
            load_config("development", config_dir=config_dir)
        text = str(info.value)
        assert "risk.max_daily_loss" in text
        assert "What to do" in text


def _bands(d: dict[str, Any]) -> list[dict[str, Any]]:
    bands: list[dict[str, Any]] = d["risk"]["drawdown_bands"]
    return bands


class TestRiskValidation:
    def test_bands_must_increase(self, config_dir: Path, patch_yaml: PatchYaml) -> None:
        patch_yaml("risk.yaml", lambda d: _bands(d)[2].update({"threshold": 0.05}))
        with pytest.raises(ConfigurationError, match="strictly increasing"):
            load_config("development", config_dir=config_dir)

    def test_caps_cannot_loosen_with_drawdown(
        self, config_dir: Path, patch_yaml: PatchYaml
    ) -> None:
        patch_yaml("risk.yaml", lambda d: _bands(d)[1].update({"max_net_exposure": 2.5}))
        with pytest.raises(ConfigurationError, match="must not loosen"):
            load_config("development", config_dir=config_dir)

    def test_deepest_band_must_block(self, config_dir: Path, patch_yaml: PatchYaml) -> None:
        patch_yaml("risk.yaml", lambda d: _bands(d)[-1].update({"block_risk_increasing": False}))
        with pytest.raises(ConfigurationError, match="deepest drawdown band"):
            load_config("development", config_dir=config_dir)

    def test_first_band_starts_at_zero(self, config_dir: Path, patch_yaml: PatchYaml) -> None:
        patch_yaml("risk.yaml", lambda d: _bands(d)[0].update({"threshold": 0.01}))
        with pytest.raises(ConfigurationError, match=r"threshold 0\.0"):
            load_config("development", config_dir=config_dir)

    def test_gross_plus_cash_floor(self, config_dir: Path, patch_yaml: PatchYaml) -> None:
        patch_yaml("risk.yaml", lambda d: d["risk"].update({"min_cash_weight": 0.2}))
        with pytest.raises(ConfigurationError, match="min_cash_weight"):
            load_config("development", config_dir=config_dir)

    def test_position_cap_for_unknown_symbol(self, config_dir: Path, patch_yaml: PatchYaml) -> None:
        patch_yaml("risk.yaml", lambda d: d["risk"]["max_position_weight"].update({"UPRO": 0.5}))
        with pytest.raises(ConfigurationError, match="unknown symbols"):
            load_config("development", config_dir=config_dir)

    def test_vol_regime_ordering(self, config_dir: Path, patch_yaml: PatchYaml) -> None:
        patch_yaml(
            "risk.yaml",
            lambda d: d["risk"]["volatility_regimes"].update({"elevated_above_pct": 0.99}),
        )
        with pytest.raises(ConfigurationError, match="strictly increasing"):
            load_config("development", config_dir=config_dir)

    def test_position_cap_lookup(self) -> None:
        risk = load_config("development", config_dir=REPO_CONFIG).settings.risk
        assert risk.position_cap("TQQQ") == pytest.approx(0.66)
        assert risk.position_cap("OTHER") == risk.default_max_position_weight


class TestUniverseAndStrategies:
    def test_duplicate_strategy_ids(self, config_dir: Path, patch_yaml: PatchYaml) -> None:
        def dup(d: dict[str, Any]) -> None:
            items = d["strategies"]["strategies"]
            items.append(dict(items[0]))

        patch_yaml("strategies.yaml", dup)
        with pytest.raises(ConfigurationError, match="duplicate strategy ids"):
            load_config("development", config_dir=config_dir)

    def test_fixed_weights_must_reference_known(
        self, config_dir: Path, patch_yaml: PatchYaml
    ) -> None:
        patch_yaml(
            "strategies.yaml",
            lambda d: d["strategies"]["ensemble"].update(
                {"method": "fixed", "fixed_weights": {"ghost": 1.0}}
            ),
        )
        with pytest.raises(ConfigurationError, match="unknown strategies"):
            load_config("development", config_dir=config_dir)

    def test_signal_symbol_must_be_declared(self, config_dir: Path, patch_yaml: PatchYaml) -> None:
        patch_yaml("base.yaml", lambda d: d["universe"].update({"signal_symbols": ["IWM"]}))
        with pytest.raises(ConfigurationError, match="not declared"):
            load_config("development", config_dir=config_dir)

    def test_options_rejected_in_v1(self, config_dir: Path, patch_yaml: PatchYaml) -> None:
        patch_yaml(
            "base.yaml",
            lambda d: d["universe"]["instruments"].append(
                {"symbol": "QQQ240119C400", "asset_class": "option"}
            ),
        )
        with pytest.raises(ConfigurationError, match="not supported in version 1"):
            load_config("development", config_dir=config_dir)

    def test_schedule_must_be_chronological(self, config_dir: Path, patch_yaml: PatchYaml) -> None:
        patch_yaml("base.yaml", lambda d: d["schedule"]["steps"].reverse())
        with pytest.raises(ConfigurationError, match="chronological"):
            load_config("development", config_dir=config_dir)

    def test_email_enabled_requires_addresses(
        self, config_dir: Path, patch_yaml: PatchYaml
    ) -> None:
        patch_yaml("base.yaml", lambda d: d["notifications"]["email"].update({"enabled": True}))
        with pytest.raises(ConfigurationError, match="email is enabled but missing"):
            load_config("development", config_dir=config_dir)


class TestFingerprint:
    def test_stable_across_loads(self) -> None:
        a = load_config("paper", config_dir=REPO_CONFIG)
        b = load_config("paper", config_dir=REPO_CONFIG)
        assert a.config_version == b.config_version

    def test_changes_when_risk_changes(self, config_dir: Path, patch_yaml: PatchYaml) -> None:
        before = load_config("paper", config_dir=config_dir).config_version
        patch_yaml("risk.yaml", lambda d: d["risk"].update({"max_daily_loss": 0.05}))
        after = load_config("paper", config_dir=config_dir).config_version
        assert before != after

    def test_comment_only_change_keeps_version(self, config_dir: Path) -> None:
        before = load_config("paper", config_dir=config_dir)
        path = config_dir / "risk.yaml"
        path.write_text(path.read_text() + "\n# a comment\n")
        after = load_config("paper", config_dir=config_dir)
        assert before.config_version == after.config_version
        assert before.sources != after.sources  # file hashes still record the edit

    def test_resolve_path(self, config_dir: Path) -> None:
        loaded = load_config("development", config_dir=config_dir)
        assert loaded.resolve_path(Path("var/x")) == (config_dir.parent / "var/x").resolve()
        assert loaded.resolve_path(Path("/abs")) == Path("/abs")


def test_deep_merge_replaces_lists_and_merges_maps() -> None:
    merged = deep_merge({"a": {"b": 1, "c": [1, 2]}, "d": 1}, {"a": {"c": [3]}, "e": 2})
    assert merged == {"a": {"b": 1, "c": [3]}, "d": 1, "e": 2}


# ------------------------------------------------------------------ live-trading policy
def _enable_live(d: dict[str, Any]) -> None:
    d["trading"] = {
        "mode": "live",
        "live_trading": {"enabled": True, "acknowledgement": LIVE_TRADING_ACKNOWLEDGEMENT},
    }


def _confirmed() -> Secrets:
    return Secrets(AQ_LIVE_TRADING_CONFIRM=LIVE_CONFIRM_ENV_VALUE)


class TestLiveTradingPolicy:
    def test_live_allowed_only_with_every_opt_in(
        self, config_dir: Path, patch_yaml: PatchYaml
    ) -> None:
        patch_yaml("production.yaml", _enable_live)
        loaded = load_config("production", config_dir=config_dir, secrets=_confirmed())
        assert loaded.settings.trading.mode is TradingMode.LIVE

    def test_missing_env_confirmation(self, config_dir: Path, patch_yaml: PatchYaml) -> None:
        patch_yaml("production.yaml", _enable_live)
        with pytest.raises(SafetyViolation, match="AQ_LIVE_TRADING_CONFIRM"):
            load_config("production", config_dir=config_dir, secrets=Secrets())

    def test_wrong_acknowledgement(self, config_dir: Path, patch_yaml: PatchYaml) -> None:
        def edit(d: dict[str, Any]) -> None:
            _enable_live(d)
            d["trading"]["live_trading"]["acknowledgement"] = "yes"

        patch_yaml("production.yaml", edit)
        with pytest.raises(SafetyViolation, match="acknowledgement"):
            load_config("production", config_dir=config_dir, secrets=_confirmed())

    def test_enabled_flag_required(self, config_dir: Path, patch_yaml: PatchYaml) -> None:
        def edit(d: dict[str, Any]) -> None:
            _enable_live(d)
            d["trading"]["live_trading"]["enabled"] = False

        patch_yaml("production.yaml", edit)
        with pytest.raises(SafetyViolation, match="enabled is false"):
            load_config("production", config_dir=config_dir, secrets=_confirmed())

    @pytest.mark.parametrize("env", ["development", "paper"])
    def test_live_forbidden_outside_production(
        self, config_dir: Path, patch_yaml: PatchYaml, env: str
    ) -> None:
        patch_yaml(f"{env}.yaml", _enable_live)
        with pytest.raises(SafetyViolation, match="requires 'production'"):
            load_config(env, config_dir=config_dir, secrets=_confirmed())

    def test_simulated_broker_cannot_go_live(self, config_dir: Path, patch_yaml: PatchYaml) -> None:
        def edit(d: dict[str, Any]) -> None:
            _enable_live(d)
            d["broker"] = {"provider": "simulated"}

        patch_yaml("production.yaml", edit)
        with pytest.raises(SafetyViolation, match="simulated"):
            load_config("production", config_dir=config_dir, secrets=_confirmed())

    def test_all_problems_reported_together(self, config_dir: Path, patch_yaml: PatchYaml) -> None:
        patch_yaml("paper.yaml", lambda d: d["trading"].update({"mode": "live"}))
        with pytest.raises(SafetyViolation) as info:
            load_config("paper", config_dir=config_dir, secrets=Secrets())
        text = str(info.value)
        for fragment in ("production", "enabled is false", "acknowledgement", "AQ_LIVE"):
            assert fragment in text

    def test_downgrade_with_stale_enable_flag_only_warns(
        self, config_dir: Path, patch_yaml: PatchYaml
    ) -> None:
        patch_yaml(
            "production.yaml", lambda d: d["trading"]["live_trading"].update({"enabled": True})
        )
        loaded = load_config("production", config_dir=config_dir)
        assert loaded.settings.trading.mode is TradingMode.PAPER
        assert any("live trading stays OFF" in w for w in loaded.warnings)

    def test_env_confirmation_alone_does_nothing(self, config_dir: Path) -> None:
        loaded = load_config("production", config_dir=config_dir, secrets=_confirmed())
        assert loaded.settings.trading.mode is TradingMode.PAPER

    def test_confirm_from_process_env(
        self, config_dir: Path, patch_yaml: PatchYaml, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        patch_yaml("production.yaml", _enable_live)
        monkeypatch.setenv("AQ_LIVE_TRADING_CONFIRM", LIVE_CONFIRM_ENV_VALUE)
        assert load_config(
            "production", config_dir=config_dir
        ).settings.trading.mode.uses_real_money


def test_secretstr_never_leaks_in_repr() -> None:
    s = Secrets(ALPACA_API_SECRET_KEY="supersecret")
    assert "supersecret" not in repr(s)
    assert isinstance(s.alpaca_api_secret_key, SecretStr)
