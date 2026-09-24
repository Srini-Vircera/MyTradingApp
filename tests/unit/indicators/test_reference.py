"""Vectorized implementations vs. slow, obviously-correct reference loops on random data."""

import math
from datetime import date

import numpy as np
import pandas as pd
import pytest

from adaptive_quant.quant.indicators.momentum import rsi
from adaptive_quant.quant.indicators.performance import drawdown, rolling_sharpe
from adaptive_quant.quant.indicators.trend import ema, rolling_zscore, sma, trend_r2, trend_slope
from adaptive_quant.quant.indicators.volatility import (
    atr,
    bollinger_bands,
    historical_volatility,
    volatility_percentile,
)
from tests.data_helpers import daily_bars

BARS = daily_bars(date(2022, 1, 3), date(2022, 12, 30), vol=0.02, seed=3)
CLOSE = BARS["close"]
X = CLOSE.to_numpy()


def windows(n: int):  # type: ignore[no-untyped-def]
    return ((i, X[i - n + 1 : i + 1]) for i in range(n - 1, len(X)))


def test_sma_and_zscore() -> None:
    m, z = sma(CLOSE, 15).to_numpy(), rolling_zscore(CLOSE, 15).to_numpy()
    for i, w in windows(15):
        assert m[i] == pytest.approx(w.mean(), rel=1e-12)
        assert z[i] == pytest.approx((w[-1] - w.mean()) / w.std(ddof=1), rel=1e-9)


def test_ema_recursion() -> None:
    n, a = 12, 2 / 13
    ref = [float("nan")] * len(X)
    ref[n - 1] = X[:n].mean()
    for i in range(n, len(X)):
        ref[i] = a * X[i] + (1 - a) * ref[i - 1]
    np.testing.assert_allclose(ema(CLOSE, n).to_numpy(), ref, rtol=1e-12, equal_nan=True)


def test_trend_slope_matches_polyfit() -> None:
    slope = trend_slope(CLOSE, 30, annualize=False).to_numpy()
    r2 = trend_r2(CLOSE, 30).to_numpy()
    for i, w in windows(30):
        y = np.log(w)
        b, a = np.polyfit(np.arange(30), y, 1)
        fitted = a + b * np.arange(30)
        ss_res, ss_tot = ((y - fitted) ** 2).sum(), ((y - y.mean()) ** 2).sum()
        assert slope[i] == pytest.approx(b, rel=1e-8)
        assert r2[i] == pytest.approx(1 - ss_res / ss_tot, rel=1e-8)


def test_historical_volatility() -> None:
    vol = historical_volatility(CLOSE, 20).to_numpy()
    rets = np.diff(np.log(X))
    for i in range(20, len(X)):
        assert vol[i] == pytest.approx(rets[i - 20 : i].std(ddof=1) * math.sqrt(252), rel=1e-9)


def test_rsi_against_textbook_loop() -> None:
    n = 14
    out = rsi(CLOSE, n).to_numpy()
    d = np.diff(X)
    ag, al = np.maximum(d[:n], 0).mean(), np.maximum(-d[:n], 0).mean()
    assert out[n] == pytest.approx(100 - 100 / (1 + ag / al), rel=1e-12)
    for i in range(n + 1, len(X)):
        ag = (ag * (n - 1) + max(d[i - 1], 0)) / n
        al = (al * (n - 1) + max(-d[i - 1], 0)) / n
        assert out[i] == pytest.approx(100 - 100 / (1 + ag / al), rel=1e-10)


def test_atr_against_loop() -> None:
    n = 10
    h, lo, c = BARS["high"].to_numpy(), BARS["low"].to_numpy(), X
    tr = [max(h[i] - lo[i], abs(h[i] - c[i - 1]), abs(lo[i] - c[i - 1])) for i in range(1, len(c))]
    out = atr(BARS, n).to_numpy()
    a = float(np.mean(tr[:n]))
    assert out[n] == pytest.approx(a, rel=1e-12)
    for i in range(n + 1, len(c)):
        a = (a * (n - 1) + tr[i - 1]) / n
        assert out[i] == pytest.approx(a, rel=1e-10)


def test_bollinger() -> None:
    bb = bollinger_bands(CLOSE, 20, 2.5)
    for i, w in windows(20):
        mid, sd = w.mean(), w.std(ddof=0)
        assert bb["upper"].iloc[i] == pytest.approx(mid + 2.5 * sd, rel=1e-10)
        assert bb["width"].iloc[i] == pytest.approx(5 * sd / mid, rel=1e-8)


def test_volatility_percentile() -> None:
    vol = historical_volatility(CLOSE, 10).to_numpy()
    pct = volatility_percentile(CLOSE, 10, 40).to_numpy()
    for i in range(10 + 40 - 1, len(X)):
        win = vol[i - 39 : i + 1]
        assert pct[i] == pytest.approx(np.mean(win <= vol[i]))


def test_drawdown_and_sharpe() -> None:
    dd = drawdown(CLOSE).to_numpy()
    peak = -np.inf
    for i, v in enumerate(X):
        peak = max(peak, v)
        assert dd[i] == pytest.approx(v / peak - 1, abs=1e-15)
    sh = rolling_sharpe(CLOSE, 30).to_numpy()
    r = X[1:] / X[:-1] - 1
    for i in range(30, len(X)):
        w = r[i - 30 : i]
        assert sh[i] == pytest.approx(w.mean() / w.std(ddof=1) * math.sqrt(252), rel=1e-8)


def test_index_is_preserved() -> None:
    out = sma(CLOSE, 5)
    assert out.index.equals(CLOSE.index)
    assert isinstance(out, pd.Series)
