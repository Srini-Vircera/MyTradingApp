"""Event-driven daily backtest engine.

Per session ``t`` (in this order):

1. fills scheduled for ``open(t)``;
2. ``near_close`` decision at ``close(t) - N min`` (data through ``close(t-1)``);
3. fills scheduled for ``close(t)`` (incl. closing-auction fills of step 5 of
   the *same* session, which run after the decision - see ``run``);
4. mark-to-market at ``close(t)`` and cash interest;
5. close-time decisions (``next_open``, ``next_close``, ``closing_auction``).

Point-in-time guarantees
------------------------
* Strategies see only rows with ``timestamp <= decision_time``
  (``Strategy.signal_at`` slices precomputed causal indicators).
* Orders are sized with prices *known at the decision time* (last visible
  close), never with the fill price.
* ADV for impact/participation uses volumes known at the decision time.
* Every fill asserts ``fill_time > data_timestamp`` unless the explicit
  ``closing_auction`` model is active; a violation raises ``LookAheadError``.
* Pending (unfilled) orders are netted when new orders are sized, as a real
  order manager would do, so decision delays never double-trade.

The engine is deterministic: no randomness, stable iteration order.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import ROUND_DOWN, Decimal

import pandas as pd

from adaptive_quant.config.schema import BacktestConfig, EnsembleConfig, RiskConfig
from adaptive_quant.core.enums import OrderSide
from adaptive_quant.core.errors import AQError, DataQualityError
from adaptive_quant.core.models import Instrument, StrategySignal
from adaptive_quant.core.money import quantize_price, to_decimal
from adaptive_quant.quant.backtest.allocation import Allocation, ExposureAllocator
from adaptive_quant.quant.backtest.costs import PRICE_TICK, CostModel
from adaptive_quant.quant.backtest.execution import (
    ExecutionTiming,
    FillPoint,
    allows_same_bar_close,
    fill_time,
    schedule,
)
from adaptive_quant.quant.backtest.portfolio import Fill, Portfolio
from adaptive_quant.quant.data.bars import require_canonical_sorted
from adaptive_quant.quant.data.calendar import Session, TradingCalendar
from adaptive_quant.quant.ensemble.engine import EnsembleEngine, EnsembleScore, Member
from adaptive_quant.quant.ensemble.shadow import ShadowBook
from adaptive_quant.quant.portfolio.manager import PortfolioDecision, PortfolioManager
from adaptive_quant.quant.portfolio.policy import AllocationPolicy
from adaptive_quant.quant.risk.engine import RiskEngine
from adaptive_quant.quant.risk.models import RiskCalculationError, RiskContext, RiskDecision
from adaptive_quant.quant.strategies.base import PrecomputedIndicators, Strategy

UNDERLYING = "QQQ"  # risk estimates and shadow returns use the unlevered underlying
WHOLE_SHARE = Decimal(1)
FRACTIONAL_STEP = Decimal("0.000001")


class LookAheadError(AQError):
    """A fill would use information not available at its decision time (engine bug)."""


@dataclass(frozen=True)
class EngineSettings:
    config: BacktestConfig
    rebalance_threshold: float
    allow_fractional: bool
    risk_limits: RiskConfig | None
    ensemble: EnsembleConfig | None = None  # M7 decision chain (default config if None)


@dataclass
class Order:
    order_id: int
    symbol: str
    side: OrderSide
    quantity: Decimal
    decision_time: datetime
    data_timestamp: datetime
    fill_index: int
    fill_point: FillPoint
    adv_shares: Decimal | None


@dataclass(frozen=True)
class Decision:
    decision_time: datetime
    data_timestamp: datetime
    session: date
    signals: tuple[StrategySignal, ...]
    net_exposure: float
    allocation: Allocation
    order_ids: tuple[int, ...]
    ensemble: EnsembleScore | None = None
    risk: RiskDecision | None = None
    refused: str | None = None  # risk engine failure: no orders this decision (fail closed)


@dataclass(frozen=True)
class CancelledOrder:
    order_id: int
    symbol: str
    side: OrderSide
    quantity: Decimal
    reason: str


@dataclass
class BacktestResult:
    daily: pd.DataFrame
    portfolio: Portfolio
    decisions: list[Decision]
    cancelled: list[CancelledOrder]
    execution: ExecutionTiming
    same_bar_close: bool
    strategy_versions: dict[str, str]
    warnings: list[str] = field(default_factory=list)

    @property
    def fills(self) -> list[Fill]:
        return self.portfolio.fills

    @property
    def contains_synthetic(self) -> bool:
        return bool(self.daily["synthetic"].any())


class BacktestEngine:
    def __init__(
        self,
        *,
        frames: Mapping[str, pd.DataFrame],
        strategies: Sequence[Strategy],
        instruments: Mapping[str, Instrument],
        calendar: TradingCalendar,
        settings: EngineSettings,
        synthetic: Mapping[str, pd.Series[bool]] | None = None,
    ) -> None:
        if not strategies:
            raise ValueError("at least one strategy is required")
        for sym, df in frames.items():
            require_canonical_sorted(df, f"{sym} bars")
        self.frames = dict(frames)
        self.strategies = list(strategies)
        self.calendar = calendar
        self.settings = settings
        self.cfg = settings.config
        self.timing = ExecutionTiming(self.cfg.execution)
        self.delay = self.cfg.execution_delay_bars
        self.allocator = ExposureAllocator(
            dict(instruments),
            self.cfg.allocation.long_mode,
            settings.risk_limits if self.cfg.allocation.apply_risk_limits else None,
        )
        self.tradeable = self.allocator.symbols
        self.manager: PortfolioManager | None = None
        a = self.cfg.allocation
        if settings.risk_limits is not None and a.apply_risk_limits and a.risk_engine:
            self.manager = PortfolioManager(
                EnsembleEngine(
                    [Member(s.strategy_id, s.family) for s in self.strategies],
                    settings.ensemble or EnsembleConfig(),
                ),
                AllocationPolicy(a.long_mode, a.min_abs_exposure),
                RiskEngine(settings.risk_limits, instruments),
            )
        self.shadow = ShadowBook([s.strategy_id for s in self.strategies])
        underlying = (
            self.frames[UNDERLYING]["close"].astype(float) if UNDERLYING in self.frames else None
        )
        self._underlying_close = underlying
        self._underlying_returns = None if underlying is None else underlying.pct_change()
        self._marks: list[float] = []
        self._band: str | None = None
        self.costs = CostModel(self.cfg.costs)
        self.synthetic = dict(synthetic or {})
        self.lot = FRACTIONAL_STEP if settings.allow_fractional else WHOLE_SHARE

    # ------------------------------------------------------------ run
    @property
    def risk_warmup_bars(self) -> int:
        """Underlying bars the risk engine needs before the first decision (0 without it)."""
        return 0 if self.manager is None else self.manager.risk.required_history + 1

    def run(self, start: date, end: date) -> BacktestResult:
        self._marks = []
        self._band = None
        self.shadow = ShadowBook([s.strategy_id for s in self.strategies])
        sessions = self.calendar.sessions_in_range(start, end)
        if len(sessions) < 2:
            raise DataQualityError(f"backtest range {start}..{end} has fewer than 2 sessions")
        px = self._price_table(sessions)
        pre: dict[str, PrecomputedIndicators] = {
            s.strategy_id: s.precompute(self.frames) for s in self.strategies
        }
        portfolio = Portfolio(to_decimal(self.cfg.initial_capital))
        pending: list[Order] = []
        decisions: list[Decision] = []
        cancelled: list[CancelledOrder] = []
        rows: list[dict[str, object]] = []
        next_id = [1]
        same_bar = allows_same_bar_close(self.timing, self.delay)

        def decide(i: int, sched_time: datetime, fill_index: int, point: FillPoint) -> None:
            if fill_index >= len(sessions):
                return  # would fill after the backtest ends; nothing to decide
            d = self._decide(
                i, sched_time, fill_index, point, sessions, px, pre, portfolio, pending, next_id
            )
            decisions.append(d)

        for i, session in enumerate(sessions):
            day_fills_start = len(portfolio.fills)
            self._fill_due(i, FillPoint.OPEN, session, px, portfolio, pending, cancelled, same_bar)
            sch = schedule(self.timing, session, self.delay, self.cfg.near_close_minutes)
            if self.timing is ExecutionTiming.NEAR_CLOSE:
                decide(i, sch.decision_time, i + sch.fill_offset, sch.fill_point)
            if self.timing is ExecutionTiming.CLOSING_AUCTION:
                decide(i, sch.decision_time, i + sch.fill_offset, sch.fill_point)
            self._fill_due(i, FillPoint.CLOSE, session, px, portfolio, pending, cancelled, same_bar)
            if i > 0:
                days = (session.date - sessions[i - 1].date).days
                rate = to_decimal(self.cfg.cash_interest_annual)
                if rate > 0:
                    portfolio.accrue_interest(portfolio.cash * rate * days / Decimal(365))
            rows.append(self._mark(i, session, px, portfolio, day_fills_start))
            self._marks.append(float(rows[-1]["equity"]))  # type: ignore[arg-type]
            if self.timing in (ExecutionTiming.NEXT_OPEN, ExecutionTiming.NEXT_CLOSE):
                decide(i, sch.decision_time, i + sch.fill_offset, sch.fill_point)

        for order in pending:
            cancelled.append(
                CancelledOrder(
                    order.order_id, order.symbol, order.side, order.quantity, "backtest ended"
                )
            )
        daily = pd.DataFrame(rows).set_index("timestamp")
        warnings = []
        if same_bar:
            warnings.append(
                "closing_auction model: signals use the same close they trade at - optimistic; "
                "requires a genuine closing-auction execution methodology"
            )
        if daily["synthetic"].any():
            warnings.append("results include SYNTHETIC price history - reported separately")
        refused = [d for d in decisions if d.refused]
        if refused:
            warnings.append(
                f"risk engine refused {len(refused)} decision(s) (no orders placed); first: "
                f"{refused[0].session}: {refused[0].refused}"
            )
        return BacktestResult(
            daily=daily,
            portfolio=portfolio,
            decisions=decisions,
            cancelled=cancelled,
            execution=self.timing,
            same_bar_close=same_bar,
            strategy_versions={s.strategy_id: s.version_id for s in self.strategies},
            warnings=warnings,
        )

    # ------------------------------------------------------------ prices
    def _price_table(self, sessions: list[Session]) -> dict[str, pd.DataFrame]:
        """Per tradeable symbol: open/close/volume aligned to the backtest sessions."""
        table: dict[str, pd.DataFrame] = {}
        closes = pd.DatetimeIndex([s.close for s in sessions])
        for sym in self.tradeable:
            if sym not in self.frames:
                raise DataQualityError(f"no price data for tradeable instrument {sym}")
            df = self.frames[sym]
            missing = closes.difference(pd.DatetimeIndex(df.index))
            if len(missing):
                raise DataQualityError(
                    f"{sym} has no bar for {len(missing)} backtest session(s), "
                    f"first {missing[0]:%Y-%m-%d}",
                    hint="start the backtest later, or enable use_synthetic_history for "
                    "pre-inception periods",
                )
            table[sym] = df.loc[closes, ["open", "close", "volume"]]
        return table

    def _known_close(self, sym: str, as_of: datetime) -> tuple[Decimal, datetime]:
        df = self.frames[sym]
        n = int(df.index.searchsorted(pd.Timestamp(as_of), side="right"))
        if n == 0:
            raise DataQualityError(f"no {sym} price known at {as_of:%Y-%m-%d %H:%M}Z")
        return _px(float(df["close"].iloc[n - 1])), df.index[n - 1].to_pydatetime()

    def _adv(self, sym: str, as_of: datetime) -> Decimal | None:
        df = self.frames[sym]
        n = int(df.index.searchsorted(pd.Timestamp(as_of), side="right"))
        w = self.cfg.costs.adv_window
        if n < w:
            return None
        vol = df["volume"].iloc[n - w : n]
        if (vol <= 0).any():
            return None
        return to_decimal(float(vol.mean()))

    # ------------------------------------------------------------ decisions
    def _decide(
        self,
        i: int,
        decision_time: datetime,
        fill_index: int,
        point: FillPoint,
        sessions: list[Session],
        px: dict[str, pd.DataFrame],
        pre: dict[str, PrecomputedIndicators],
        portfolio: Portfolio,
        pending: list[Order],
        next_id: list[int],
    ) -> Decision:
        signals = tuple(s.signal_at(pre[s.strategy_id], decision_time) for s in self.strategies)
        data_ts = max(sig.data_timestamp for sig in signals)
        if data_ts > decision_time:  # defence in depth (StrategySignal also enforces it)
            raise LookAheadError(f"signal data {data_ts} after decision {decision_time}")
        known = {sym: self._known_close(sym, decision_time) for sym in self.tradeable}
        prices = {sym: p for sym, (p, _) in known.items()}
        for sym, (_, ts) in known.items():
            if ts > decision_time:
                raise LookAheadError(f"{sym} sizing price from {ts} after decision {decision_time}")
        equity = portfolio.equity(prices)
        ens: EnsembleScore | None = None
        risk: RiskDecision | None = None
        if self.manager is None:
            net = sum(sig.suggested_exposure for sig in signals) / len(signals)
            allocation = self.allocator.allocate(net)
        else:
            try:
                chain = self._decide_chain(
                    signals, data_ts, decision_time, prices, equity, portfolio, pending
                )
            except RiskCalculationError as exc:
                return Decision(
                    decision_time=decision_time,
                    data_timestamp=data_ts,
                    session=sessions[i].date,
                    signals=signals,
                    net_exposure=math.nan,
                    allocation=Allocation(math.nan, {}, (f"REFUSED: {exc}",)),
                    order_ids=(),
                    refused=str(exc),
                )
            ens, risk = chain.ensemble, chain.risk
            net = ens.exposure
            self._band = risk.band
            notes = [
                f"band {risk.band} (drawdown {risk.drawdown:.1%})",
                f"vol scale {risk.vol_scale:.3f}",
                *(f"flag: {f}" for f in risk.flags),
                *ens.weights.adjustments,
                *(str(a) for a in risk.adjustments),
            ]
            allocation = Allocation(net, dict(risk.weights), tuple(notes))
        investable = equity * (1 - to_decimal(self.cfg.sizing_cash_buffer))
        threshold = to_decimal(self.settings.rebalance_threshold)
        order_ids: list[int] = []
        for sym in self.tradeable:
            pend = sum(
                (
                    o.quantity if o.side is OrderSide.BUY else -o.quantity
                    for o in pending
                    if o.symbol == sym
                ),
                Decimal(0),
            )
            projected = portfolio.quantity(sym) + pend
            target_w = allocation.weights.get(sym, Decimal(0))
            target_qty = (target_w * investable / prices[sym]).quantize(
                self.lot, rounding=ROUND_DOWN
            )
            delta = target_qty - projected
            if delta == 0:
                continue
            current_w = projected * prices[sym] / equity if equity > 0 else Decimal(0)
            if target_w > 0 and abs(target_w - current_w) < threshold:
                continue  # within the rebalance band (full exits always go through)
            order = Order(
                order_id=next_id[0],
                symbol=sym,
                side=OrderSide.BUY if delta > 0 else OrderSide.SELL,
                quantity=abs(delta),
                decision_time=decision_time,
                data_timestamp=data_ts,
                fill_index=fill_index,
                fill_point=point,
                adv_shares=self._adv(sym, decision_time),
            )
            next_id[0] += 1
            pending.append(order)
            order_ids.append(order.order_id)
        return Decision(
            decision_time=decision_time,
            data_timestamp=data_ts,
            session=sessions[i].date,
            signals=signals,
            net_exposure=net,
            allocation=allocation,
            order_ids=tuple(order_ids),
            ensemble=ens,
            risk=risk,
        )

    def _decide_chain(
        self,
        signals: tuple[StrategySignal, ...],
        data_ts: datetime,
        decision_time: datetime,
        prices: dict[str, Decimal],
        equity: Decimal,
        portfolio: Portfolio,
        pending: list[Order],
    ) -> PortfolioDecision:
        """Ensemble -> policy -> risk engine, with a strictly point-in-time context."""
        if (
            self.manager is None
            or self._underlying_close is None
            or self._underlying_returns is None
        ):
            raise RiskCalculationError(f"risk engine needs {UNDERLYING} history")
        closes, rets = self._underlying_close, self._underlying_returns
        n = int(closes.index.searchsorted(pd.Timestamp(decision_time), side="right"))
        if n and closes.index[n - 1] > pd.Timestamp(decision_time):  # pragma: no cover - defensive
            raise LookAheadError("underlying history beyond the decision time")
        self.shadow.record(data_ts, {s.strategy_name: s.suggested_exposure for s in signals})
        eq = float(equity)
        current: dict[str, float] = {}
        for sym in self.tradeable:
            qty = portfolio.quantity(sym) + sum(
                (
                    o.quantity if o.side is OrderSide.BUY else -o.quantity
                    for o in pending
                    if o.symbol == sym
                ),
                Decimal(0),
            )
            current[sym] = max(float(qty * prices[sym]) / eq, 0.0) if eq > 0 else 0.0
        initial = float(self.cfg.initial_capital)
        ctx = RiskContext(
            as_of=decision_time,
            equity=eq,
            peak_equity=max([initial, eq, *self._marks]),
            previous_equity=self._marks[-2] if len(self._marks) >= 2 else initial,
            current_weights=current,
            underlying_returns=rets.iloc[1:n].to_numpy(dtype=float),
            kill_switch_engaged=False,
            previous_band=self._band,
        )
        return self.manager.decide(signals, self.shadow.returns(closes, decision_time), ctx)

    # ------------------------------------------------------------ fills
    def _fill_due(
        self,
        i: int,
        point: FillPoint,
        session: Session,
        px: dict[str, pd.DataFrame],
        portfolio: Portfolio,
        pending: list[Order],
        cancelled: list[CancelledOrder],
        same_bar: bool,
    ) -> None:
        due = [o for o in pending if o.fill_index == i and o.fill_point is point]
        if not due:
            return
        for o in due:
            pending.remove(o)
        when = fill_time(session, point)
        # sells first so their proceeds can fund buys
        for order in sorted(due, key=lambda o: (o.side is OrderSide.BUY, o.order_id)):
            self._check_timing(order, when, same_bar)
            ref = _px(float(px[order.symbol][point.value].iloc[i]))
            self._execute(order, ref, when, portfolio, cancelled)

    def _check_timing(self, order: Order, when: datetime, same_bar: bool) -> None:
        if when < order.decision_time:
            raise LookAheadError(f"order {order.order_id} fills before it was decided")
        if when > order.data_timestamp:
            return
        if same_bar and when == order.data_timestamp:
            return  # explicit closing-auction model
        raise LookAheadError(
            f"order {order.order_id} ({order.symbol}) would fill at {when:%Y-%m-%d %H:%M}Z "
            f"using data from {order.data_timestamp:%Y-%m-%d %H:%M}Z"
        )

    def _execute(
        self,
        order: Order,
        ref: Decimal,
        when: datetime,
        portfolio: Portfolio,
        cancelled: list[CancelledOrder],
    ) -> None:
        qty, limited = order.quantity, ""
        cap = self.costs.participation_cap(order.adv_shares)
        if cap is None and order.adv_shares is None and self.cfg.costs.unknown_volume == "reject":
            cancelled.append(
                CancelledOrder(order.order_id, order.symbol, order.side, qty, "volume unknown")
            )
            return
        if cap is not None:
            cap = cap.quantize(self.lot, rounding=ROUND_DOWN)
            if qty > cap:
                qty, limited = cap, "participation"
        if order.side is OrderSide.SELL:
            qty = min(qty, portfolio.quantity(order.symbol))
        else:
            qty = self._affordable(order, qty, ref, portfolio)
            if qty < order.quantity and not limited:
                limited = "cash"
        if qty <= 0:
            cancelled.append(
                CancelledOrder(
                    order.order_id,
                    order.symbol,
                    order.side,
                    order.quantity,
                    limited or "nothing to trade",
                )
            )
            return
        quote = self.costs.quote(order.symbol, order.side, qty, ref, order.adv_shares)
        fill = Fill(
            order_id=order.order_id,
            time=when,
            decision_time=order.decision_time,
            data_timestamp=order.data_timestamp,
            symbol=order.symbol,
            side=order.side,
            requested_quantity=order.quantity,
            quantity=qty,
            ref_price=ref,
            fill_price=quote.fill_price,
            notional=quote.notional,
            commission=quote.commission,
            spread_cost=quote.spread_cost,
            slippage_cost=quote.slippage_cost,
            impact_cost=quote.impact_cost,
            limited_by=limited,
        )
        portfolio.apply(fill, quote)
        if qty < order.quantity:
            cancelled.append(
                CancelledOrder(
                    order.order_id,
                    order.symbol,
                    order.side,
                    order.quantity - qty,
                    f"unfilled remainder ({limited})",
                )
            )

    def _affordable(
        self, order: Order, qty: Decimal, ref: Decimal, portfolio: Portfolio
    ) -> Decimal:
        """Largest quantity <= qty whose cost (incl. commission) fits in cash."""

        def cost(q: Decimal) -> Decimal:
            quote = self.costs.quote(order.symbol, order.side, q, ref, order.adv_shares)
            return quote.notional + quote.commission

        if qty <= 0 or cost(qty) <= portfolio.cash:
            return qty
        lo, hi = Decimal(0), qty
        for _ in range(60):  # bisection on the lot grid
            mid = ((lo + hi) / 2).quantize(self.lot, rounding=ROUND_DOWN)
            if mid <= lo:
                break
            if cost(mid) <= portfolio.cash:
                lo = mid
            else:
                hi = mid
        return lo

    # ------------------------------------------------------------ marking
    def _mark(
        self,
        i: int,
        session: Session,
        px: dict[str, pd.DataFrame],
        portfolio: Portfolio,
        fills_from: int,
    ) -> dict[str, object]:
        prices = {sym: _px(float(px[sym]["close"].iloc[i])) for sym in self.tradeable}
        gap = portfolio.identity_gap(prices)
        if gap != 0:
            raise AQError(f"accounting identity violated by {gap} on {session.date}")
        equity = portfolio.equity(prices)
        values = {sym: portfolio.quantity(sym) * prices[sym] for sym in self.tradeable}
        weights = {sym: (v / equity if equity > 0 else Decimal(0)) for sym, v in values.items()}
        todays = portfolio.fills[fills_from:]
        traded = sum((f.notional for f in todays), Decimal(0))
        row: dict[str, object] = {
            "timestamp": pd.Timestamp(session.close),
            "equity": float(equity),
            "cash": float(portfolio.cash),
            "gross_exposure": float(sum(weights.values(), Decimal(0))),
            "net_exposure": self.allocator.net_exposure(weights),
            "turnover": float(traded / equity) if equity > 0 else 0.0,
            "commissions": float(sum((f.commission for f in todays), Decimal(0))),
            "implicit_costs": float(
                sum((f.spread_cost + f.slippage_cost + f.impact_cost for f in todays), Decimal(0))
            ),
            "synthetic": any(
                bool(flags.get(pd.Timestamp(session.close), False))
                for sym, flags in self.synthetic.items()
                if sym in self.tradeable
            ),
        }
        for sym in self.tradeable:
            row[f"w_{sym}"] = float(weights[sym])
        return row


def _px(value: float) -> Decimal:
    """Bar price as Decimal on the engine's price tick (reference, sizing and marking alike)."""
    return quantize_price(to_decimal(value), PRICE_TICK)
