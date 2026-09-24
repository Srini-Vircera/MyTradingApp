"""Identifiers.

``client_order_id`` is *deterministic*: the same order intent (same trading
cycle, symbol, side and sequence number) always produces the same ID. Brokers
reject a second order with an ID they have already seen, so a retry after a
timeout or a restart cannot create a duplicate order.
"""

from __future__ import annotations

import hashlib
import re
import secrets
from datetime import datetime

from adaptive_quant.core.clock import ensure_utc
from adaptive_quant.core.enums import OrderSide

CLIENT_ORDER_ID_PREFIX = "aq"
_MAX_CLIENT_ORDER_ID_LEN = 48  # conservative: fits Alpaca (128) and IBKR orderRef limits
_SAFE = re.compile(r"^[A-Za-z0-9_.-]+$")


def new_run_id(now: datetime, prefix: str = "run") -> str:
    """Sortable, unique ID such as ``run-20240102T204500Z-9f2c1a7b``."""
    stamp = ensure_utc(now).strftime("%Y%m%dT%H%M%SZ")
    return f"{prefix}-{stamp}-{secrets.token_hex(4)}"


def trading_cycle_id(session_date: str, environment: str) -> str:
    """One trading cycle per session date per environment, e.g. ``cyc-paper-2024-01-02``.

    Using the date (not a random value) means a restarted process resumes the
    *same* cycle and therefore regenerates the *same* client order IDs.
    """
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", session_date):
        raise ValueError(f"session_date must be YYYY-MM-DD, got {session_date!r}")
    if not _SAFE.fullmatch(environment):
        raise ValueError(f"environment contains unsafe characters: {environment!r}")
    return f"cyc-{environment}-{session_date}"


def client_order_id(cycle_id: str, symbol: str, side: OrderSide, sequence: int = 0) -> str:
    """Deterministic, broker-safe client order ID for one order intent.

    ``sequence`` distinguishes deliberate additional orders for the same
    symbol/side within a cycle (e.g. a replacement after a confirmed cancel).
    It must be allocated from persisted state, never from a retry loop.
    """
    if sequence < 0:
        raise ValueError("sequence must be non-negative")
    if not symbol or not _SAFE.fullmatch(symbol):
        raise ValueError(f"invalid symbol for order id: {symbol!r}")
    payload = f"{cycle_id}|{symbol.upper()}|{side.value}|{sequence}"
    digest = hashlib.sha256(payload.encode()).hexdigest()[:20]
    cid = f"{CLIENT_ORDER_ID_PREFIX}-{symbol.upper()}-{side.value[0]}{sequence}-{digest}"
    if len(cid) > _MAX_CLIENT_ORDER_ID_LEN:
        raise ValueError(f"client order id too long: {cid}")
    return cid
