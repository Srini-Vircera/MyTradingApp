from datetime import date
from pathlib import Path

import pandas as pd
import pytest

from adaptive_quant.core.clock import FrozenClock
from adaptive_quant.core.errors import CorporateActionsUnavailable
from adaptive_quant.quant.data.bars import Adjustment, Frequency, SeriesKey
from adaptive_quant.quant.data.corporate_actions import ActionType, CorporateAction
from adaptive_quant.quant.data.normalize import restrict_dates
from adaptive_quant.quant.data.pipeline import DataPipeline
from adaptive_quant.quant.data.providers.base import (
    BarRequest,
    MarketDataProvider,
    ProviderCapabilities,
)
from adaptive_quant.quant.data.store import ParquetBarStore
from adaptive_quant.quant.data.validation import BarValidator, IssueKind
from tests.data_helpers import calendar, daily_bars, market_dates

START, END = date(2024, 1, 2), date(2024, 3, 28)
SPLIT_DAY = date(2024, 2, 15)


class FakeProvider(MarketDataProvider):
    def __init__(
        self,
        bars: pd.DataFrame,
        actions: list[CorporateAction] | None,
        adjustments: frozenset[Adjustment] = frozenset({Adjustment.RAW}),
    ) -> None:
        self.bars = bars
        self.actions = actions
        self.adjustments = adjustments

    @property
    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities("fake", frozenset(Frequency), self.adjustments, True)

    def fetch_bars(self, request: BarRequest) -> pd.DataFrame:
        return restrict_dates(self.bars, request.start, request.end)

    def fetch_corporate_actions(self, symbol: str, start: date, end: date) -> list[CorporateAction]:
        if self.actions is None:
            raise CorporateActionsUnavailable("not available")
        return self.actions


def raw_with_split() -> pd.DataFrame:
    df = daily_bars(START, END)
    after = market_dates(df) >= SPLIT_DAY
    df.loc[after, ["open", "high", "low", "close"]] /= 2
    df.loc[after, "volume"] *= 2
    return df


SPLIT = CorporateAction(symbol="QQQ", ex_date=SPLIT_DAY, type=ActionType.SPLIT, ratio=2.0)


@pytest.fixture
def store(tmp_path: Path, clock: FrozenClock) -> ParquetBarStore:
    clock.set(pd.Timestamp("2024-06-01", tz="UTC").to_pydatetime())
    return ParquetBarStore(tmp_path / "data", clock)


def pipeline(
    provider: MarketDataProvider, store: ParquetBarStore, clock: FrozenClock
) -> DataPipeline:
    return DataPipeline(provider, store, BarValidator(calendar()), clock)


def key(adj: Adjustment) -> SeriesKey:
    return SeriesKey("fake", "QQQ", Frequency.DAILY, adj)


def test_stores_raw_and_derived_series(store: ParquetBarStore, clock: FrozenClock) -> None:
    result = pipeline(FakeProvider(raw_with_split(), [SPLIT]), store, clock).update(
        "QQQ", Frequency.DAILY, START, END
    )
    assert result.ok, result.summary()
    assert set(result.snapshots) == {Adjustment.RAW, Adjustment.SPLIT, Adjustment.ALL}
    assert result.corporate_actions == 1
    split_adj = store.read(key(Adjustment.SPLIT))["close"]
    assert split_adj.pct_change().abs().max() < 0.2  # the split jump is gone
    assert store.read_corporate_actions("fake", "QQQ") == [SPLIT]


def test_rerun_is_idempotent(store: ParquetBarStore, clock: FrozenClock) -> None:
    p = pipeline(FakeProvider(raw_with_split(), [SPLIT]), store, clock)
    p.update("QQQ", Frequency.DAILY, START, END)
    p.update("QQQ", Frequency.DAILY, START, END)
    assert all(len(store.history(key(a))) == 1 for a in Adjustment)


def test_incremental_update_merges_and_detects_revisions(
    store: ParquetBarStore, clock: FrozenClock
) -> None:
    full = raw_with_split()
    pipeline(FakeProvider(full.iloc[:40], [SPLIT]), store, clock).update(
        "QQQ", Frequency.DAILY, START, END
    )
    revised = full.copy()
    revised.loc[revised.index[35], "close"] *= 1.001  # vendor revised an old bar
    revised.loc[revised.index[35], "high"] *= 1.002
    result = pipeline(FakeProvider(revised.iloc[30:], [SPLIT]), store, clock).update(
        "QQQ", Frequency.DAILY, START, END
    )
    assert result.revisions == 1
    assert any("revised" in n for n in result.notes)
    assert len(store.read(key(Adjustment.RAW))) == len(full)


def test_invalid_data_is_stored_but_not_served_and_not_adjusted(
    store: ParquetBarStore, clock: FrozenClock
) -> None:
    bad = raw_with_split()
    bad.loc[bad.index[10], "close"] = -1.0
    result = pipeline(FakeProvider(bad, [SPLIT]), store, clock).update(
        "QQQ", Frequency.DAILY, START, END
    )
    assert not result.ok
    assert set(result.snapshots) == {Adjustment.RAW}
    assert store.latest(key(Adjustment.RAW)) is None
    assert store.latest(key(Adjustment.RAW), require_valid=False) is not None


def test_missing_corporate_actions_is_recorded(store: ParquetBarStore, clock: FrozenClock) -> None:
    result = pipeline(FakeProvider(raw_with_split(), None), store, clock).update(
        "QQQ", Frequency.DAILY, START, END
    )
    assert result.corporate_actions is None
    assert any("unavailable" in n for n in result.notes)
    assert IssueKind.UNEXPLAINED_JUMP in result.report.kinds()
    assert store.read_corporate_actions("fake", "QQQ") is None


def test_unordered_provider_data_is_not_stored(store: ParquetBarStore, clock: FrozenClock) -> None:
    df = raw_with_split().iloc[::-1]
    result = pipeline(FakeProvider(df, [SPLIT]), store, clock).update(
        "QQQ", Frequency.DAILY, START, END
    )
    assert not result.ok
    assert result.snapshots == {}
    assert IssueKind.OUT_OF_ORDER in result.report.kinds()


def test_empty_fetch(store: ParquetBarStore, clock: FrozenClock) -> None:
    result = pipeline(FakeProvider(raw_with_split().iloc[0:0], [SPLIT]), store, clock).update(
        "QQQ", Frequency.DAILY, START, END
    )
    assert not result.ok
    assert IssueKind.EMPTY in result.report.kinds()


def test_adjusted_only_provider_stores_single_series(
    store: ParquetBarStore, clock: FrozenClock
) -> None:
    provider = FakeProvider(daily_bars(START, END), [], frozenset({Adjustment.ALL}))
    result = pipeline(provider, store, clock).update("QQQ", Frequency.DAILY, START, END)
    assert result.ok
    assert set(result.snapshots) == {Adjustment.ALL}
    assert any("no local adjustment" in n for n in result.notes)
