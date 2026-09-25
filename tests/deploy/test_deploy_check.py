"""``aq deploy check``: a container refuses to start unless it is deployment-safe."""

from __future__ import annotations

from dataclasses import replace
from typing import Any

import pytest

from adaptive_quant.cli import main
from adaptive_quant.cli_deploy import problems
from adaptive_quant.config.loader import LoadedConfig, load_config
from adaptive_quant.config.secrets import Secrets
from adaptive_quant.core.enums import TradingMode
from tests.conftest import REPO_CONFIG

TOKEN = "k" * 40
DB = "postgresql://aq:secret@postgres.railway.internal:5432/railway"


def prod() -> LoadedConfig:
    return load_config("production", config_dir=REPO_CONFIG)


def secrets(**extra: str) -> Secrets:
    return Secrets(**{"DATABASE_URL": DB, "AQ_API_TOKEN": TOKEN, **extra})  # type: ignore[arg-type]


def edit(loaded: LoadedConfig, **trading: Any) -> LoadedConfig:
    s = loaded.settings
    t = s.trading.model_copy(update=trading)
    return replace(loaded, settings=s.model_copy(update={"trading": t}))


def test_shipped_production_config_passes_for_both_roles() -> None:
    assert problems(prod(), secrets(), "api", {}) == []
    assert problems(prod(), secrets(), "worker", {}) == []
    assert prod().settings.trading.mode is TradingMode.SHADOW


_T = prod().settings.trading


@pytest.mark.parametrize(
    ("change", "fragment"),
    [
        ({"mode": TradingMode.LIVE}, "trading.mode is live"),
        ({"live_trading": _T.live_trading.model_copy(update={"enabled": True})}, "enabled"),
        ({"kill_switch": _T.kill_switch.model_copy(update={"store": "file"})}, "kill_switch"),
    ],
)
def test_refuses_unsafe_trading_config(change: dict[str, Any], fragment: str) -> None:
    found = problems(edit(prod(), **change), secrets(), "api", {})
    assert any(fragment in p for p in found), found


def test_refuses_the_live_confirmation_variable_even_if_empty() -> None:
    found = problems(prod(), secrets(), "worker", {"AQ_LIVE_TRADING_CONFIRM": ""})
    assert any("AQ_LIVE_TRADING_CONFIRM" in p for p in found)


def test_refuses_development_and_missing_database() -> None:
    dev = load_config("development", config_dir=REPO_CONFIG)
    found = problems(dev, Secrets(AQ_API_TOKEN=TOKEN), "api", {})
    text = " | ".join(found)
    for fragment in ("development", "json", "kill_switch.store", "DATABASE_URL"):
        assert fragment in text


def test_api_needs_a_strong_token() -> None:
    weak = Secrets(DATABASE_URL=DB, AQ_API_TOKEN="short")
    assert any("AQ_API_TOKEN" in p for p in problems(prod(), weak, "api", {}))
    assert problems(prod(), weak, "worker", {}) == []  # the worker does not serve the API


def test_scheduler_needs_paper_broker_credentials() -> None:
    env = {"AQ_SCHEDULER_ENABLED": "true"}
    assert any("ALPACA_API_KEY_ID" in p for p in problems(prod(), secrets(), "worker", env))
    keys = secrets(ALPACA_API_KEY_ID="id", ALPACA_API_SECRET_KEY="sk")
    assert problems(prod(), keys, "worker", env) == []
    backtest = edit(prod(), mode=TradingMode.BACKTEST)
    assert any("shadow or paper" in p for p in problems(backtest, keys, "worker", env))


def test_refuses_a_non_paper_broker_endpoint() -> None:
    s = prod().settings
    alpaca = s.broker.alpaca.model_copy(update={"paper_base_url": "https://api.alpaca.markets"})
    broker = s.broker.model_copy(update={"alpaca": alpaca})
    loaded = replace(prod(), settings=s.model_copy(update={"broker": broker}))
    assert any("paper-api.alpaca.markets" in p for p in problems(loaded, secrets(), "api", {}))


def test_cli_exit_codes(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("DATABASE_URL", DB)
    monkeypatch.setenv("AQ_API_TOKEN", TOKEN)
    monkeypatch.delenv("AQ_LIVE_TRADING_CONFIRM", raising=False)
    args = ["--env", "production", "--config-dir", str(REPO_CONFIG), "--env-file", "/nonexistent"]
    assert main([*args, "deploy", "check", "--role", "api"]) == 0
    assert "live trading disabled" in capsys.readouterr().out
    monkeypatch.setenv("AQ_LIVE_TRADING_CONFIRM", "yes-trade-real-money")
    assert main([*args, "deploy", "check", "--role", "api"]) == 2
    out = capsys.readouterr().out
    assert "REFUSED" in out and DB not in out and TOKEN not in out  # never echoes secrets
