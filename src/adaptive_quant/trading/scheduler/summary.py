"""Plain-text end-of-day summary (email body and log)."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date
from decimal import Decimal

from adaptive_quant.core.enums import TradingMode


def end_of_day_summary(
    *,
    session_date: date,
    mode: TradingMode,
    equity: Decimal,
    pnl: Decimal,
    daily_return: float,
    drawdown: float,
    steps: Mapping[str, tuple[str, str]],
    fills: Sequence[tuple[str, str, Decimal]],
    positions: Mapping[str, Decimal],
    reconciled: bool,
) -> str:
    money = "PAPER - no real money" if not mode.uses_real_money else "LIVE"
    lines = [
        f"End of day {session_date} ({mode.value}; {money})",
        "",
        f"Equity            {equity:,.2f}",
        f"P&L today         {pnl:+,.2f} ({daily_return:+.2%})",
        f"Drawdown          {drawdown:.2%} from peak",
        "Reconciliation    "
        + ("passed" if reconciled else "FAILED - risk-increasing trading blocked"),
        "",
        "Fills:" if fills else "Fills: none",
        *(f"  {side:<4} {qty} {sym}" for sym, side, qty in fills),
        "",
        "Positions:" if positions else "Positions: none (all cash)",
        *(f"  {sym:<5} {qty}" for sym, qty in sorted(positions.items())),
        "",
        "Cycle steps:",
        *(f"  {name:<20} {status:<8} {detail}" for name, (status, detail) in steps.items()),
        "",
        "Results are from paper/simulated trading and are not a prediction of future returns.",
    ]
    return "\n".join(lines)
