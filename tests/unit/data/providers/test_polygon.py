from datetime import UTC, date, datetime

import httpx
import pytest
from pydantic import SecretStr

from adaptive_quant.core.errors import DataProviderError
from adaptive_quant.quant.data.bars import Adjustment, Frequency
from adaptive_quant.quant.data.corporate_actions import ActionType
from adaptive_quant.quant.data.providers.base import BarRequest
from adaptive_quant.quant.data.providers.polygon import PolygonDataProvider
from tests.data_helpers import calendar
from tests.unit.data.providers.fixtures import Recorder, load


def route(request: httpx.Request) -> httpx.Response:
    path = request.url.path
    if path.startswith("/v2/aggs/ticker/QQQ/range/1/day/1704430800000"):
        return httpx.Response(200, json=load("polygon_aggs_daily_page2.json"))
    if path.startswith("/v2/aggs/ticker/QQQ/range/1/day/"):
        return httpx.Response(200, json=load("polygon_aggs_daily_page1.json"))
    if path.startswith("/v2/aggs/ticker/I:NDX/"):
        return httpx.Response(200, json={"status": "OK", "resultsCount": 0})
    if path == "/v3/reference/splits":
        return httpx.Response(200, json=load("polygon_splits.json"))
    if path == "/v3/reference/dividends":
        return httpx.Response(200, json=load("polygon_dividends.json"))
    return httpx.Response(404)


def provider(rec: Recorder) -> PolygonDataProvider:
    return PolygonDataProvider(
        api_key=SecretStr("POLYKEY"),
        base_url="https://api.polygon.io",
        timeout_seconds=5,
        calendar=calendar(),
        symbol_map={"NDX": "I:NDX"},
        transport=rec.transport,
    )


def test_daily_aggregates_follow_next_url_with_header_auth() -> None:
    rec = Recorder(route)
    bars = provider(rec).fetch_bars(
        BarRequest("QQQ", Frequency.DAILY, date(2024, 1, 2), date(2024, 1, 5))
    )
    assert len(rec.requests) == 2
    assert all(r.headers["Authorization"] == "Bearer POLYKEY" for r in rec.requests)
    assert all("POLYKEY" not in str(r.url) for r in rec.requests)
    assert rec.requests[0].url.params["adjusted"] == "false"
    assert list(bars.index) == [datetime(2024, 1, d, 21, tzinfo=UTC) for d in (2, 3, 4, 5)]
    assert bars["close"].iloc[-1] == 397.44


def test_split_adjusted_request_and_unsupported_total_return() -> None:
    rec = Recorder(route)
    p = provider(rec)
    p.fetch_bars(
        BarRequest("QQQ", Frequency.DAILY, date(2024, 1, 2), date(2024, 1, 5), Adjustment.SPLIT)
    )
    assert rec.requests[0].url.params["adjusted"] == "true"
    with pytest.raises(DataProviderError, match="derived locally"):
        p.fetch_bars(
            BarRequest("QQQ", Frequency.DAILY, date(2024, 1, 2), date(2024, 1, 5), Adjustment.ALL)
        )


def test_index_symbol_mapping_and_empty_results() -> None:
    rec = Recorder(route)
    bars = provider(rec).fetch_bars(
        BarRequest("NDX", Frequency.DAILY, date(2024, 1, 2), date(2024, 1, 5))
    )
    assert bars.empty
    assert "/I:NDX/" in rec.requests[0].url.path


def test_corporate_actions_skip_zero_amounts() -> None:
    actions = provider(Recorder(route)).fetch_corporate_actions(
        "TQQQ", date(2020, 1, 1), date(2024, 1, 1)
    )
    assert [(a.type, a.ex_date) for a in actions] == [
        (ActionType.SPLIT, date(2022, 1, 13)),
        (ActionType.CASH_DIVIDEND, date(2023, 12, 20)),
    ]
    assert actions[0].ratio == 2.0
    assert actions[1].amount == pytest.approx(0.0127)


def test_error_status_rejected() -> None:
    rec = Recorder(lambda r: httpx.Response(200, json={"status": "ERROR", "error": "bad"}))
    with pytest.raises(DataProviderError, match="unexpected status"):
        provider(rec).fetch_bars(
            BarRequest("QQQ", Frequency.DAILY, date(2024, 1, 2), date(2024, 1, 5))
        )


def test_repeated_next_url_aborts() -> None:
    page = {"status": "OK", "results": [], "next_url": "https://api.polygon.io/v2/aggs/again"}
    rec = Recorder(lambda r: httpx.Response(200, json=page))
    with pytest.raises(DataProviderError, match="repeated"):
        provider(rec).fetch_bars(
            BarRequest("QQQ", Frequency.DAILY, date(2024, 1, 2), date(2024, 1, 5))
        )
