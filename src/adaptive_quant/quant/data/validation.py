"""Market-data validation.

:class:`BarValidator` inspects a bar frame *as delivered* and reports every
problem it finds, grouped by kind. ERROR issues make the report fail
(``report.ok is False``); consumers must then refuse to use the data. WARNING
issues are recorded and surfaced but do not block.

Detected defect classes
-----------------------
============================  ========  ==================================================
kind                          severity  meaning
============================  ========  ==================================================
``empty``                     error     no rows
``missing_value``             error     NaN / infinite value in a required column
``non_positive_price``        error     price <= 0 (zero prices included)
``impossible_price``          error     high < low, or open/close outside [low, high]
``negative_volume``           error     volume < 0
``bad_timestamp``             error     not on the bar grid (daily: not a session close;
                                        intraday: misaligned or outside the session)
``future_timestamp``          error     bar ends after the evaluation time (look-ahead)
``out_of_order``              error     timestamps not strictly increasing as delivered
``duplicate``                 error     the same timestamp appears more than once
``non_session``               error     daily bar dated on a weekend/holiday
``missing_bar``               error     expected session (daily) has no bar
``missing_intraday_bar``      warning   expected intraday bar absent (thin trading happens)
``unexplained_jump``          warning   |close-to-close return| above the configured limit
                                        without a corporate action on that date
``vwap_outside_range``        warning   vwap outside [low, high]
``tracking_error_exceeded``   error     synthetic series deviates from the real fund
                                        beyond the configured bound (set by history.py)
============================  ========  ==================================================

Staleness is evaluated separately (``freshness.py``) because it depends on the
wall-clock time at which data is *used*, not on the data itself.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date, datetime
from enum import StrEnum

import numpy as np
import pandas as pd

from adaptive_quant.core.clock import MARKET_TZ, ensure_utc
from adaptive_quant.core.errors import DataQualityError
from adaptive_quant.quant.data.bars import (
    PRICE_COLUMNS,
    REQUIRED_COLUMNS,
    Frequency,
    index_ns,
    to_canonical,
)
from adaptive_quant.quant.data.calendar import TradingCalendar

_REL_TOL = 1e-9
_MAX_EXAMPLES = 5


class IssueSeverity(StrEnum):
    WARNING = "warning"
    ERROR = "error"


class IssueKind(StrEnum):
    EMPTY = "empty"
    MISSING_VALUE = "missing_value"
    NON_POSITIVE_PRICE = "non_positive_price"
    IMPOSSIBLE_PRICE = "impossible_price"
    NEGATIVE_VOLUME = "negative_volume"
    BAD_TIMESTAMP = "bad_timestamp"
    FUTURE_TIMESTAMP = "future_timestamp"
    OUT_OF_ORDER = "out_of_order"
    DUPLICATE = "duplicate"
    NON_SESSION = "non_session"
    MISSING_BAR = "missing_bar"
    MISSING_INTRADAY_BAR = "missing_intraday_bar"
    UNEXPLAINED_JUMP = "unexplained_jump"
    VWAP_OUTSIDE_RANGE = "vwap_outside_range"
    TRACKING_ERROR_EXCEEDED = "tracking_error_exceeded"


_SEVERITY = {
    IssueKind.MISSING_INTRADAY_BAR: IssueSeverity.WARNING,
    IssueKind.UNEXPLAINED_JUMP: IssueSeverity.WARNING,
    IssueKind.VWAP_OUTSIDE_RANGE: IssueSeverity.WARNING,
}


@dataclass(frozen=True)
class DataIssue:
    kind: IssueKind
    severity: IssueSeverity
    count: int
    message: str
    examples: tuple[str, ...] = ()

    def __str__(self) -> str:
        ex = f" e.g. {', '.join(self.examples)}" if self.examples else ""
        return f"[{self.severity}] {self.kind} x{self.count}: {self.message}{ex}"


@dataclass(frozen=True)
class ValidationReport:
    subject: str
    rows: int
    first: datetime | None
    last: datetime | None
    issues: tuple[DataIssue, ...] = field(default=())

    @property
    def errors(self) -> list[DataIssue]:
        return [i for i in self.issues if i.severity is IssueSeverity.ERROR]

    @property
    def warnings(self) -> list[DataIssue]:
        return [i for i in self.issues if i.severity is IssueSeverity.WARNING]

    @property
    def ok(self) -> bool:
        return not self.errors

    def kinds(self) -> set[IssueKind]:
        return {i.kind for i in self.issues}

    def summary(self) -> str:
        span = f"{self.first:%Y-%m-%d} .. {self.last:%Y-%m-%d}" if self.first and self.last else "-"
        head = f"{self.subject}: {self.rows} rows ({span}) - " + (
            "OK" if self.ok else f"{len(self.errors)} error kind(s)"
        )
        return "\n".join([head, *(f"  {i}" for i in self.issues)])

    def raise_if_invalid(self) -> None:
        if not self.ok:
            raise DataQualityError(
                self.summary(), hint="fix or re-download the data; invalid data is never used"
            )


@dataclass(frozen=True)
class ValidationPolicy:
    max_abs_return: float = 0.5
    check_missing_bars: bool = True


class BarValidator:
    def __init__(self, calendar: TradingCalendar, policy: ValidationPolicy | None = None) -> None:
        self.calendar = calendar
        self.policy = policy or ValidationPolicy()

    def validate(
        self,
        bars: pd.DataFrame,
        *,
        frequency: Frequency,
        subject: str,
        corporate_action_dates: Iterable[date] = (),
        as_of: datetime | None = None,
        expected_start: date | None = None,
        expected_end: date | None = None,
    ) -> ValidationReport:
        """Validate ``bars``; never raises for data problems (they become issues)."""
        try:
            df = to_canonical(bars)
        except DataQualityError as exc:
            return ValidationReport(
                subject,
                len(bars),
                None,
                None,
                (DataIssue(IssueKind.BAD_TIMESTAMP, IssueSeverity.ERROR, 1, exc.message),),
            )
        c = _Collector()
        if df.empty:
            c.add(IssueKind.EMPTY, 1, "no bars")
            return ValidationReport(subject, 0, None, None, c.issues())

        self._check_order(df, c)
        clean = df[~df.index.duplicated(keep="first")].sort_index()
        self._check_values(clean, c)
        if as_of is not None:
            self._check_future(clean, ensure_utc(as_of), c)
        if frequency is Frequency.DAILY:
            self._check_daily_grid(clean, c, expected_start, expected_end)
            self._check_jumps(clean, set(corporate_action_dates), c)
        else:
            self._check_intraday_grid(clean, frequency, c)

        first = clean.index[0].to_pydatetime()
        last = clean.index[-1].to_pydatetime()
        return ValidationReport(subject, len(df), first, last, c.issues())

    # ---------------------------------------------------------------- checks
    @staticmethod
    def _check_order(df: pd.DataFrame, c: _Collector) -> None:
        idx = pd.DatetimeIndex(df.index)
        dup = idx.duplicated(keep="first")
        if dup.any():
            c.add(IssueKind.DUPLICATE, int(dup.sum()), "repeated timestamps", idx[dup])
        diffs = np.diff(index_ns(idx))
        backwards = np.flatnonzero(diffs < 0)
        if backwards.size:
            c.add(
                IssueKind.OUT_OF_ORDER,
                int(backwards.size),
                "timestamps decrease",
                idx[backwards + 1],
            )

    @staticmethod
    def _check_values(df: pd.DataFrame, c: _Collector) -> None:
        values = df[list(REQUIRED_COLUMNS)]
        bad = ~np.isfinite(values.to_numpy()).all(axis=1)
        if bad.any():
            c.add(IssueKind.MISSING_VALUE, int(bad.sum()), "NaN/inf in OHLCV", df.index[bad])
        prices = df[list(PRICE_COLUMNS)]
        nonpos = (prices <= 0).any(axis=1).to_numpy()
        if nonpos.any():
            c.add(IssueKind.NON_POSITIVE_PRICE, int(nonpos.sum()), "price <= 0", df.index[nonpos])
        hi, lo = df["high"], df["low"]
        tol = _REL_TOL * hi.abs()
        impossible = np.asarray(
            (hi < lo - tol)
            | (df["open"] > hi + tol)
            | (df["open"] < lo - tol)
            | (df["close"] > hi + tol)
            | (df["close"] < lo - tol)
        )
        if impossible.any():
            c.add(
                IssueKind.IMPOSSIBLE_PRICE,
                int(impossible.sum()),
                "high < low or open/close outside [low, high]",
                df.index[impossible],
            )
        negvol = (df["volume"] < 0).to_numpy()
        if negvol.any():
            c.add(IssueKind.NEGATIVE_VOLUME, int(negvol.sum()), "volume < 0", df.index[negvol])
        if "vwap" in df.columns:
            v = df["vwap"]
            outside = (v.notna() & ((v > hi + tol) | (v < lo - tol))).to_numpy()
            if outside.any():
                c.add(
                    IssueKind.VWAP_OUTSIDE_RANGE,
                    int(outside.sum()),
                    "vwap outside [low, high]",
                    df.index[outside],
                )

    @staticmethod
    def _check_future(df: pd.DataFrame, as_of: datetime, c: _Collector) -> None:
        future = np.asarray(df.index > pd.Timestamp(as_of))
        if future.any():
            c.add(
                IssueKind.FUTURE_TIMESTAMP,
                int(future.sum()),
                f"bars end after {as_of:%Y-%m-%d %H:%M}Z (not yet knowable)",
                df.index[future],
            )

    def _check_daily_grid(
        self,
        df: pd.DataFrame,
        c: _Collector,
        expected_start: date | None,
        expected_end: date | None,
    ) -> None:
        cal = self.calendar
        dates = [ts.astimezone(MARKET_TZ).date() for ts in df.index]
        non_session: list[pd.Timestamp] = []
        misaligned: list[pd.Timestamp] = []
        for ts, d in zip(df.index, dates, strict=True):
            session = cal.session(d)
            if session is None:
                non_session.append(ts)
            elif ts.to_pydatetime() != session.close:
                misaligned.append(ts)
        if non_session:
            c.add(IssueKind.NON_SESSION, len(non_session), "bars on non-trading days", non_session)
        if misaligned:
            c.add(
                IssueKind.BAD_TIMESTAMP,
                len(misaligned),
                "daily bars must be stamped at the session close",
                misaligned,
            )
        if not self.policy.check_missing_bars:
            return
        start = expected_start or dates[0]
        end = expected_end or dates[-1]
        present = set(dates)
        missing = [s.date for s in cal.sessions_in_range(start, end) if s.date not in present]
        if missing:
            c.add(
                IssueKind.MISSING_BAR,
                len(missing),
                f"sessions without a bar between {start} and {end}",
                missing,
            )

    def _check_intraday_grid(self, df: pd.DataFrame, freq: Frequency, c: _Collector) -> None:
        step = freq.duration
        by_date: dict[date, list[pd.Timestamp]] = {}
        bad: list[pd.Timestamp] = []
        for ts in df.index:
            by_date.setdefault(ts.astimezone(MARKET_TZ).date(), []).append(ts)
        expected_total = 0
        missing: list[pd.Timestamp] = []
        for d, stamps in by_date.items():
            session = self.calendar.session(d)
            if session is None:
                bad.extend(stamps)
                continue
            grid = list(pd.date_range(session.open + step, session.close, freq=step))
            if not grid or grid[-1] != pd.Timestamp(session.close):
                grid.append(pd.Timestamp(session.close))
            grid_set = set(grid)
            bad.extend(ts for ts in stamps if ts not in grid_set)
            if self.policy.check_missing_bars:
                have = set(stamps)
                missing.extend(g for g in grid if g not in have)
                expected_total += len(grid)
        if bad:
            c.add(
                IssueKind.BAD_TIMESTAMP,
                len(bad),
                f"intraday bars must end on the {freq} grid inside a session",
                bad,
            )
        if missing:
            c.add(
                IssueKind.MISSING_INTRADAY_BAR,
                len(missing),
                f"{len(missing)} of {expected_total} expected bars absent",
                missing,
            )

    def _check_jumps(self, df: pd.DataFrame, action_dates: set[date], c: _Collector) -> None:
        close = df["close"]
        rets = close / close.shift(1) - 1.0
        big = rets.abs() > self.policy.max_abs_return
        flagged = [
            ts
            for ts in df.index[big.to_numpy()]
            if ts.astimezone(MARKET_TZ).date() not in action_dates
        ]
        if flagged:
            c.add(
                IssueKind.UNEXPLAINED_JUMP,
                len(flagged),
                f"close-to-close move above {self.policy.max_abs_return:.0%} "
                "without a corporate action (possible bad tick or missing split)",
                flagged,
            )


class _Collector:
    def __init__(self) -> None:
        self._issues: list[DataIssue] = []

    def add(
        self,
        kind: IssueKind,
        count: int,
        message: str,
        where: Iterable[object] = (),
    ) -> None:
        examples = tuple(_fmt(w) for _, w in zip(range(_MAX_EXAMPLES), where, strict=False))
        severity = _SEVERITY.get(kind, IssueSeverity.ERROR)
        self._issues.append(DataIssue(kind, severity, count, message, examples))

    def issues(self) -> tuple[DataIssue, ...]:
        return tuple(self._issues)


def _fmt(value: object) -> str:
    if isinstance(value, datetime):
        return (
            value.strftime("%Y-%m-%d %H:%M") if value.hour or value.minute else f"{value:%Y-%m-%d}"
        )
    return str(value)
