"""Point-in-time "shadow" returns of each strategy's own suggestions.

For consecutive decisions k, k+1 (data timestamps t_k < t_k+1), strategy i's
shadow return is ``exposure_i(k) x (close(t_k+1) / close(t_k) - 1)`` of the
underlying: a frictionless proxy of what following the strategy alone earned.
Every value is known once ``t_k+1`` is visible, so ensemble weights computed
from it never look ahead. Costs and leveraged-ETF path effects are ignored
(documented approximation, used only to *weight* strategies).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime

import numpy as np
import pandas as pd


class ShadowBook:
    def __init__(self, strategy_ids: Sequence[str]) -> None:
        self.ids = list(strategy_ids)
        self._ts: list[pd.Timestamp] = []
        self._exp: list[list[float]] = []
        self._rows: list[list[float]] = []  # completed intervals (incremental)
        self._closes_id: int | None = None
        self._close: dict[pd.Timestamp, float] = {}

    def record(self, data_timestamp: datetime, exposures: Mapping[str, float]) -> None:
        ts = pd.Timestamp(data_timestamp)
        if self._ts and ts <= self._ts[-1]:
            return  # same data seen again (e.g. delayed decisions): keep the first
        self._ts.append(ts)
        self._exp.append([float(exposures[i]) for i in self.ids])

    def returns(self, closes: pd.Series, upto: datetime) -> np.ndarray:
        """(T x N) shadow returns for every completed interval ending at or before ``upto``."""
        if self._closes_id != id(closes):  # new price source: rebuild from scratch
            self._closes_id = id(closes)
            self._close = dict(
                zip(pd.DatetimeIndex(closes.index), closes.to_numpy(dtype=float), strict=True)
            )
            self._rows = []
        cutoff = pd.Timestamp(upto)
        while len(self._rows) < len(self._ts) - 1 and self._ts[len(self._rows) + 1] <= cutoff:
            k = len(self._rows)
            r = self._close[self._ts[k + 1]] / self._close[self._ts[k]] - 1.0
            self._rows.append([e * r for e in self._exp[k]])
        n = sum(1 for k in range(len(self._rows)) if self._ts[k + 1] <= cutoff)
        return np.array(self._rows[:n], dtype=float).reshape(-1, len(self.ids))
