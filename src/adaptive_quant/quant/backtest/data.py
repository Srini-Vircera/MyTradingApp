"""Load backtest price histories from the store, keeping synthetic data labelled.

All histories are **total-return adjusted** daily bars (``Adjustment.ALL``), for
both signals and accounting (dividends are therefore reinvested implicitly).
With ``use_synthetic`` the leveraged products are extended backwards with
validated synthetic history; every row's origin is kept in ``synthetic``.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field

import pandas as pd

from adaptive_quant.core.errors import MissingDataError
from adaptive_quant.quant.data.bars import Adjustment, Frequency, SeriesKey
from adaptive_quant.quant.data.history import extended_history
from adaptive_quant.quant.data.store import ParquetBarStore

ADJUSTMENT = Adjustment.ALL


@dataclass
class BacktestData:
    frames: dict[str, pd.DataFrame]
    synthetic: dict[str, pd.Series[bool]] = field(default_factory=dict)
    provenance: dict[str, str] = field(default_factory=dict)
    missing_optional: list[str] = field(default_factory=list)


def load_backtest_data(
    store: ParquetBarStore,
    source: str,
    required: Iterable[str],
    optional: Iterable[str] = (),
    *,
    use_synthetic: bool = False,
    synthetic_symbols: Iterable[str] = (),
) -> BacktestData:
    data = BacktestData(frames={})
    syn_set = set(synthetic_symbols)
    req = list(dict.fromkeys(required))
    for symbol in [*req, *[s for s in optional if s not in req]]:
        try:
            if use_synthetic and symbol in syn_set:
                ext = extended_history(store, symbol, source=source)
                flags = ext["is_synthetic"].astype(bool)
                frame = ext.drop(columns=["is_synthetic"])
                data.synthetic[symbol] = flags
                n_syn = int(flags.sum())
                data.provenance[symbol] = f"{source}:{symbol}:1d:all" + (
                    f" + {n_syn} SYNTHETIC rows" if n_syn else ""
                )
            else:
                key = SeriesKey(source, symbol, Frequency.DAILY, ADJUSTMENT)
                frame = store.read(key)
                info = store.latest(key)
                data.provenance[symbol] = f"{key} #{info.content_hash[:12] if info else '?'}"
                data.synthetic[symbol] = pd.Series(False, index=frame.index)
        except MissingDataError:
            if symbol in req:
                raise
            data.missing_optional.append(symbol)
            continue
        data.frames[symbol] = frame
    return data
