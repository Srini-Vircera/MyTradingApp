"""Buy-and-hold benchmark curves (total return, no costs) and a cash benchmark."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass

import pandas as pd


@dataclass(frozen=True)
class Benchmark:
    name: str
    equity: pd.Series[float]
    synthetic: pd.Series[bool]


def benchmark_curves(
    frames: Mapping[str, pd.DataFrame],
    symbols: Iterable[str],
    index: pd.DatetimeIndex,
    initial: float,
    *,
    cash_rate: float = 0.0,
    synthetic: Mapping[str, pd.Series[bool]] | None = None,
) -> tuple[list[Benchmark], list[str]]:
    """Curves normalized to ``initial`` at the first backtest session.

    A benchmark lacking data for any backtest session is skipped with a note,
    never partially plotted (that would compare different periods).
    """
    out: list[Benchmark] = []
    notes: list[str] = []
    syn = dict(synthetic or {})
    for sym in symbols:
        df = frames.get(sym)
        if df is None:
            notes.append(f"{sym} benchmark unavailable: no data loaded")
            continue
        missing = index.difference(pd.DatetimeIndex(df.index))
        if len(missing):
            notes.append(
                f"{sym} benchmark unavailable for the full period "
                f"(no data before {pd.DatetimeIndex(df.index)[0]:%Y-%m-%d})"
            )
            continue
        close = df["close"].reindex(index)
        flags = (
            syn.get(sym, pd.Series(False, index=df.index)).reindex(index).fillna(False).astype(bool)
        )
        out.append(Benchmark(f"{sym} buy & hold", initial * close / close.iloc[0], flags))
    days = pd.Series(index.to_series().diff().dt.days.fillna(0).to_numpy(), index=index)
    growth = (1 + cash_rate * days / 365).cumprod()
    out.append(Benchmark("Cash", initial * growth, pd.Series(False, index=index)))
    return out, notes
