from datetime import UTC, date, datetime

import numpy as np
import pytest

from adaptive_quant.core.errors import DataQualityError, MissingDataError
from adaptive_quant.quant.data.view import MarketDataView
from adaptive_quant.quant.indicators.engine import IndicatorEngine
from adaptive_quant.quant.indicators.specs import REGISTRY, IndicatorSpec
from tests.data_helpers import daily_bars

BARS = daily_bars(date(2023, 1, 3), date(2024, 1, 31), seed=8)
JAN10_1545 = datetime(2024, 1, 10, 20, 45, tzinfo=UTC)  # before the Jan 10 close


class TestSpecs:
    def test_defaults_are_filled_and_named(self) -> None:
        spec = IndicatorSpec("bollinger")
        assert dict(spec.params) == {"window": 20, "num_std": 2.0, "output": "width"}
        assert spec.name == "bollinger_20_2_width"
        assert IndicatorSpec("sma", {"window": 200}).name == "sma_200"
        assert IndicatorSpec("rolling_high", {"window": 20, "include_current": False}).name == (
            "rolling_high_20_f"
        )
        assert IndicatorSpec("sma", {"window": 5}, source="high").name == "sma_5_high"
        assert IndicatorSpec("sma", {"window": 5}, name="fast").name == "fast"

    @pytest.mark.parametrize(
        ("kind", "params", "source", "match"),
        [
            ("wma", {}, None, "unknown indicator kind"),
            ("sma", {"period": 5}, None, "unknown parameter"),
            ("sma", {"window": 0}, None, "window"),
            ("sma", {"window": "20"}, None, "integer"),
            ("sma", {"window": 5}, "vwap", "source"),
            ("bollinger", {"output": "mid"}, None, "output"),
            ("bollinger", {"num_std": "2"}, None, "number"),
            ("rolling_high", {"include_current": 1}, None, "true/false"),
            ("momentum", {"window": 5, "skip": 7}, None, "skip"),
        ],
    )
    def test_invalid_specs_fail_at_construction(
        self, kind: str, params: dict[str, object], source: str | None, match: str
    ) -> None:
        with pytest.raises(ValueError, match=match):
            IndicatorSpec(kind, params, source=source)  # type: ignore[arg-type]

    def test_specs_are_immutable(self) -> None:
        spec = IndicatorSpec("sma", {"window": 5})
        with pytest.raises(TypeError):
            spec.params["window"] = 6  # type: ignore[index]
        assert spec.warmup == 4

    def test_volume_source_for_volume_confirmation(self) -> None:
        spec = IndicatorSpec("sma", {"window": 3}, source="volume")
        assert spec.name == "sma_3_volume"
        out = spec.compute(BARS)
        assert out.iloc[2] == pytest.approx(BARS["volume"].iloc[:3].mean())

    def test_registry_describes_every_kind(self) -> None:
        assert all(d.description for d in REGISTRY.values())


class TestEngine:
    ENGINE = IndicatorEngine(
        [
            IndicatorSpec("sma", {"window": 20}),
            IndicatorSpec("rsi", {"window": 14}),
            IndicatorSpec("atr", {"window": 14}),
            IndicatorSpec("volatility_percentile", {"vol_window": 20, "lookback": 60}),
        ]
    )

    def test_compute_all_columns(self) -> None:
        frame = self.ENGINE.compute(BARS)
        assert list(frame.columns) == ["sma_20", "rsi_14", "atr_14", "volatility_percentile_20_60"]
        assert self.ENGINE.warmup == 79

    def test_snapshot_is_point_in_time_and_matches_full_compute(self) -> None:
        snap = self.ENGINE.snapshot(BARS, JAN10_1545)
        assert snap.data_timestamp == datetime(2024, 1, 9, 21, 0, tzinfo=UTC)  # not Jan 10
        full = self.ENGINE.compute(BARS).loc[snap.data_timestamp]
        for name, value in snap.values.items():
            assert value == pytest.approx(full[name], rel=1e-9)
        assert snap.ready

    def test_snapshot_view_equals_snapshot(self) -> None:
        view = MarketDataView({"QQQ": BARS}, JAN10_1545)
        assert self.ENGINE.snapshot_view(view, "QQQ") == self.ENGINE.snapshot(BARS, JAN10_1545)

    def test_warming_up_values_are_none(self) -> None:
        early = datetime(2023, 2, 15, 22, 0, tzinfo=UTC)
        snap = self.ENGINE.snapshot(BARS, early)
        assert snap.values["sma_20"] is not None
        assert snap.values["volatility_percentile_20_60"] is None
        assert not snap.ready
        with pytest.raises(MissingDataError, match="volatility_percentile"):
            snap.require("sma_20", "volatility_percentile_20_60")
        assert set(snap.require("sma_20")) == {"sma_20"}

    def test_no_bars_before_as_of(self) -> None:
        with pytest.raises(MissingDataError):
            self.ENGINE.snapshot(BARS, datetime(2020, 1, 1, tzinfo=UTC))

    def test_duplicate_names_rejected(self) -> None:
        with pytest.raises(ValueError, match="duplicate"):
            IndicatorEngine(
                [IndicatorSpec("sma", {"window": 5}), IndicatorSpec("sma", {"window": 5})]
            )

    def test_unsorted_bars_rejected(self) -> None:
        with pytest.raises(DataQualityError):
            self.ENGINE.compute(BARS.iloc[::-1])

    def test_snapshot_never_sees_future_rows(self) -> None:
        """Even a deliberately corrupted future cannot influence a snapshot."""
        poisoned = BARS.copy()
        after = poisoned.index > JAN10_1545
        poisoned.loc[after, ["open", "high", "low", "close"]] = 1e9
        a = self.ENGINE.snapshot(BARS, JAN10_1545)
        b = self.ENGINE.snapshot(poisoned, JAN10_1545)
        assert a == b
        assert np.isfinite(list(a.require(*a.values).values())).all()

    def test_empty_engine(self) -> None:
        engine = IndicatorEngine([])
        assert engine.warmup == 0
        assert engine.compute(BARS).empty
