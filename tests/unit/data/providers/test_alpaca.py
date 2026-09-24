from datetime import UTC, date, datetime

import httpx
import pytest
from pydantic import SecretStr

from adaptive_quant.config.schema import AlpacaConfig
from adaptive_quant.core.errors import DataProviderError
from adaptive_quant.quant.data.bars import Adjustment, Frequency
from adaptive_quant.quant.data.corporate_actions import ActionType
from adaptive_quant.quant.data.providers.alpaca import AlpacaDataProvider
from adaptive_quant.quant.data.providers.base import BarRequest
from adaptive_quant.quant.data.validation import BarValidator
from tests.data_helpers import calendar
from tests.unit.data.providers.fixtures import Recorder, load


def route(request: httpx.Request) -> httpx.Response:
    path = request.url.path
    if path == "/v2/stocks/QQQ/bars":
        if request.url.params.get("timeframe") == "5Min":
            return httpx.Response(200, json=load("alpaca_bars_5min.json"))
        page = 2 if request.url.params.get("page_token") else 1
        return httpx.Response(200, json=load(f"alpaca_bars_daily_page{page}.json"))
    if path == "/v1/corporate-actions":
        return httpx.Response(200, json=load("alpaca_corporate_actions.json"))
    return httpx.Response(404, text="not found")


def provider(recorder: Recorder) -> AlpacaDataProvider:
    return AlpacaDataProvider(
        key_id=SecretStr("PKTEST"),
        secret_key=SecretStr("SECRETTEST"),
        config=AlpacaConfig(),
        calendar=calendar(),
        transport=recorder.transport,
    )


def test_daily_bars_paginate_and_stamp_at_session_close() -> None:
    rec = Recorder(route)
    bars = provider(rec).fetch_bars(
        BarRequest("QQQ", Frequency.DAILY, date(2024, 1, 2), date(2024, 1, 5))
    )
    assert len(rec.requests) == 2  # followed next_page_token
    assert rec.requests[1].url.params["page_token"].startswith("UVFR")
    assert list(bars.index) == [
        datetime(2024, 1, d, 21, tzinfo=UTC) for d in (2, 3, 4, 5)
    ]  # Jan 8 trimmed
    assert bars["close"].tolist() == [402.58, 398.67, 396.93, 397.44]
    report = BarValidator(calendar()).validate(bars, frequency=Frequency.DAILY, subject="QQQ")
    assert report.ok, report.summary()


def test_request_parameters_and_auth_headers() -> None:
    rec = Recorder(route)
    provider(rec).fetch_bars(BarRequest("QQQ", Frequency.DAILY, date(2024, 1, 2), date(2024, 1, 5)))
    req = rec.requests[0]
    assert req.url.params["adjustment"] == "raw"
    assert req.url.params["feed"] == "iex"
    assert req.url.params["end"] == "2024-01-06"
    assert req.headers["APCA-API-KEY-ID"] == "PKTEST"
    assert "SECRETTEST" not in str(req.url)


def test_intraday_bars_drop_extended_hours_and_stamp_bar_end() -> None:
    rec = Recorder(route)
    bars = provider(rec).fetch_bars(
        BarRequest("QQQ", Frequency.MINUTE_5, date(2024, 1, 2), date(2024, 1, 2))
    )
    assert list(bars.index) == [
        datetime(2024, 1, 2, 14, 35, tzinfo=UTC),
        datetime(2024, 1, 2, 14, 40, tzinfo=UTC),
        datetime(2024, 1, 2, 21, 0, tzinfo=UTC),
    ]


def test_corporate_actions_parsed_and_filtered() -> None:
    rec = Recorder(route)
    actions = provider(rec).fetch_corporate_actions("TQQQ", date(2020, 1, 1), date(2024, 1, 1))
    assert [(a.type, a.ex_date) for a in actions] == [
        (ActionType.SPLIT, date(2022, 1, 13)),
        (ActionType.CASH_DIVIDEND, date(2023, 12, 20)),
    ]
    assert actions[0].ratio == 2.0
    assert rec.requests[0].url.params["types"] == "forward_split,reverse_split,cash_dividend"


def test_empty_response() -> None:
    rec = Recorder(lambda r: httpx.Response(200, json={"bars": None, "next_page_token": None}))
    bars = provider(rec).fetch_bars(
        BarRequest("QQQ", Frequency.DAILY, date(2024, 1, 2), date(2024, 1, 5))
    )
    assert bars.empty


def test_repeated_page_token_aborts() -> None:
    rec = Recorder(lambda r: httpx.Response(200, json={"bars": [], "next_page_token": "same"}))
    with pytest.raises(DataProviderError, match="repeated"):
        provider(rec).fetch_bars(
            BarRequest("QQQ", Frequency.DAILY, date(2024, 1, 2), date(2024, 1, 5))
        )


def test_malformed_payload() -> None:
    rec = Recorder(lambda r: httpx.Response(200, json={"bars": [{"t": "2024-01-02T05:00:00Z"}]}))
    with pytest.raises(DataProviderError, match="malformed"):
        provider(rec).fetch_bars(
            BarRequest("QQQ", Frequency.DAILY, date(2024, 1, 2), date(2024, 1, 5))
        )


def test_capabilities() -> None:
    caps = provider(Recorder(route)).capabilities
    assert caps.corporate_actions
    assert Adjustment.ALL in caps.adjustments
