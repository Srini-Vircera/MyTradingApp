"""Corporate actions and price adjustment.

Adjusted series are *derived locally* from raw bars plus corporate actions,
using the standard backward (CRSP-style) method, so the adjustment methodology
does not depend on which vendor supplied the data:

* **Split** with ratio ``r`` (new shares per old share; 3-for-1 -> 3, 1-for-4
  reverse -> 0.25) effective on ``ex_date``: every earlier price is divided by
  ``r`` and every earlier volume multiplied by ``r``.
* **Cash distribution** ``D`` per share on ``ex_date``: every earlier price is
  multiplied by ``1 - D / P`` where ``P`` is the *raw* close of the last session
  before ``ex_date``. Volumes are unchanged.

Factors compound multiplicatively. Both kinds of factor are unit-free, so the
order in which actions are applied does not matter. With this (industry
standard) convention the adjusted ex-date return is ``P_t / (P - D) - 1``,
which differs from ``(P_t + D) / P - 1`` only at second order.
"""

from __future__ import annotations

import math
from collections.abc import Iterable
from datetime import date
from enum import StrEnum
from typing import Self

import numpy as np
import pandas as pd
from pydantic import BaseModel, ConfigDict, model_validator

from adaptive_quant.core.clock import MARKET_TZ
from adaptive_quant.core.errors import DataQualityError
from adaptive_quant.quant.data.bars import PRICE_COLUMNS, Adjustment, require_canonical_sorted


class ActionType(StrEnum):
    SPLIT = "split"
    CASH_DIVIDEND = "cash_dividend"


class CorporateAction(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    symbol: str
    ex_date: date
    type: ActionType
    ratio: float | None = None  # splits: new shares per old share
    amount: float | None = None  # dividends: cash per share, in the trading currency
    source: str = "unknown"

    @model_validator(mode="after")
    def _check(self) -> Self:
        if self.type is ActionType.SPLIT:
            if self.ratio is None or not math.isfinite(self.ratio) or self.ratio <= 0:
                raise ValueError(f"{self.symbol} {self.ex_date}: split ratio must be positive")
            if self.ratio == 1:
                raise ValueError(f"{self.symbol} {self.ex_date}: split ratio of 1 is a no-op")
            if self.amount is not None:
                raise ValueError("splits must not carry an amount")
        else:
            if self.amount is None or not math.isfinite(self.amount) or self.amount <= 0:
                raise ValueError(f"{self.symbol} {self.ex_date}: dividend amount must be positive")
            if self.ratio is not None:
                raise ValueError("dividends must not carry a ratio")
        return self

    @property
    def identity(self) -> tuple[str, date, ActionType, float | None, float | None]:
        """Source-independent identity used for de-duplication."""
        return (self.symbol, self.ex_date, self.type, self.ratio, self.amount)


def dedupe_actions(actions: Iterable[CorporateAction]) -> list[CorporateAction]:
    """Drop exact duplicates (same symbol, date, type and values); sort by date."""
    seen: dict[tuple[str, date, ActionType, float | None, float | None], CorporateAction] = {}
    for a in actions:
        seen.setdefault(a.identity, a)
    return sorted(seen.values(), key=lambda a: (a.ex_date, a.type.value))


def adjustment_factors(
    raw: pd.DataFrame, actions: Iterable[CorporateAction], mode: Adjustment
) -> tuple[pd.Series[float], pd.Series[float]]:
    """Return ``(price_factor, volume_factor)`` per row for converting raw -> ``mode``."""
    require_canonical_sorted(raw, "raw bars")
    n = len(raw)
    price = np.ones(n)
    volume = np.ones(n)
    if mode is Adjustment.RAW or n == 0:
        return _series(price, raw), _series(volume, raw)

    dates = np.array([ts.astimezone(MARKET_TZ).date() for ts in raw.index])
    closes = raw["close"].to_numpy()
    for action in dedupe_actions(actions):
        before = dates < action.ex_date
        if not before.any() or before.all():
            continue  # action outside the data range: nothing to adjust
        if action.type is ActionType.SPLIT and action.ratio is not None:
            price[before] /= action.ratio
            volume[before] *= action.ratio
        elif mode is Adjustment.ALL and action.amount is not None:
            prev_close = float(closes[before][-1])
            factor = 1.0 - action.amount / prev_close
            if factor <= 0:
                raise DataQualityError(
                    f"{action.symbol} {action.ex_date}: dividend {action.amount} is not "
                    f"smaller than the prior close {prev_close}",
                    hint="the corporate action or the price data is wrong; check the source",
                )
            price[before] *= factor
    return _series(price, raw), _series(volume, raw)


def apply_adjustment(
    raw: pd.DataFrame, actions: Iterable[CorporateAction], mode: Adjustment
) -> pd.DataFrame:
    """Derive an adjusted bar frame from raw bars."""
    price_f, volume_f = adjustment_factors(raw, list(actions), mode)
    out = raw.copy()
    for col in (*PRICE_COLUMNS, "vwap"):
        if col in out.columns:
            out[col] = out[col] * price_f
    out["volume"] = out["volume"] * volume_f
    return out


def _series(values: np.ndarray, like: pd.DataFrame) -> pd.Series[float]:
    return pd.Series(values, index=like.index, dtype="float64")
