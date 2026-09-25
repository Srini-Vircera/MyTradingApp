"""Causal risk estimators over a returns array whose last element is the latest known."""

from __future__ import annotations

import math

import numpy as np

from adaptive_quant.config.schema import VolatilityRegimeConfig

P = 252


def ewma_volatility(r: np.ndarray, span: int) -> float:
    """Annualised EWMA volatility (zero mean, decay 2/(span+1)); needs >= ``span`` returns."""
    x = np.asarray(r, dtype=float)
    if len(x) < span:
        raise ValueError(f"volatility estimate needs {span} returns, have {len(x)}")
    x = x[-min(len(x), 5 * span) :]
    if not np.isfinite(x).all():
        raise ValueError("non-finite return in volatility window")
    alpha = 2.0 / (span + 1.0)
    w = (1 - alpha) ** np.arange(len(x))[::-1]
    return math.sqrt(float((w * x * x).sum() / w.sum()) * P)


def volatility_regime(r: np.ndarray, cfg: VolatilityRegimeConfig) -> tuple[str, float]:
    """(regime, percentile of the current realised vol within the lookback)."""
    x = np.asarray(r, dtype=float)
    need = cfg.lookback_days + cfg.vol_window - 1
    if len(x) < need:
        raise ValueError(f"volatility regime needs {need} returns, have {len(x)}")
    x = x[-need:]
    if not np.isfinite(x).all():
        raise ValueError("non-finite return in regime window")
    windows = np.lib.stride_tricks.sliding_window_view(x, cfg.vol_window)
    vols = windows.std(axis=1, ddof=1)
    # mid-rank: ties count half, so a flat history reads as 'normal', not 'extreme'
    pct = float(((vols < vols[-1]).sum() + 0.5 * (vols == vols[-1]).sum()) / len(vols))
    if pct > cfg.extreme_above_pct:
        regime = "extreme"
    elif pct > cfg.elevated_above_pct:
        regime = "elevated"
    elif pct < cfg.low_below_pct:
        regime = "low"
    else:
        regime = "normal"
    return regime, pct


def required_history(vt_enabled: bool, vt_lookback: int, regime: VolatilityRegimeConfig) -> int:
    """Underlying returns the risk engine needs before it can approve anything."""
    need = vt_lookback if vt_enabled else 0
    if regime.max_net_exposure:
        need = max(need, regime.lookback_days + regime.vol_window - 1)
    return need
