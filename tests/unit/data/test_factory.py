from datetime import date

import pytest

from adaptive_quant.config.loader import load_config
from adaptive_quant.config.secrets import Secrets
from adaptive_quant.core.errors import ConfigurationError, MissingSecretError
from adaptive_quant.quant.data.bars import Frequency
from adaptive_quant.quant.data.factory import build_provider
from adaptive_quant.quant.data.providers.alpaca import AlpacaDataProvider
from adaptive_quant.quant.data.providers.base import BarRequest
from adaptive_quant.quant.data.providers.files import FileDataProvider
from adaptive_quant.quant.data.providers.polygon import PolygonDataProvider
from tests.conftest import REPO_CONFIG
from tests.data_helpers import calendar

LOADED = load_config("development", config_dir=REPO_CONFIG)


def test_builds_each_provider() -> None:
    keys = Secrets(ALPACA_API_KEY_ID="k", ALPACA_API_SECRET_KEY="s", POLYGON_API_KEY="p")
    assert isinstance(build_provider("file", LOADED, Secrets(), calendar()), FileDataProvider)
    alpaca = build_provider("alpaca", LOADED, keys, calendar())
    polygon = build_provider("polygon", LOADED, keys, calendar())
    assert isinstance(alpaca, AlpacaDataProvider)
    assert isinstance(polygon, PolygonDataProvider)
    assert polygon.ticker("NDX") == "I:NDX"  # symbol map comes from config
    alpaca.close()
    polygon.close()


@pytest.mark.parametrize(
    ("name", "var"), [("alpaca", "ALPACA_API_KEY_ID"), ("polygon", "POLYGON_API_KEY")]
)
def test_network_providers_require_credentials(name: str, var: str) -> None:
    with pytest.raises(MissingSecretError, match=var):
        build_provider(name, LOADED, Secrets(), calendar())


def test_unknown_provider() -> None:
    with pytest.raises(ConfigurationError, match="unknown data provider"):
        build_provider("yahoo", LOADED, Secrets(), calendar())


def test_bar_request_validation() -> None:
    with pytest.raises(ValueError, match="before start"):
        BarRequest("QQQ", Frequency.DAILY, date(2024, 2, 1), date(2024, 1, 1))
    with pytest.raises(ValueError, match="empty"):
        BarRequest("", Frequency.DAILY, date(2024, 1, 1), date(2024, 1, 2))
