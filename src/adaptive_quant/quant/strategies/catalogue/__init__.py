"""Candidate strategy implementations. Importing this package registers them all."""

from adaptive_quant.quant.strategies.catalogue import (  # noqa: F401
    benchmark,
    breakout,
    mean_reversion,
    momentum,
    regime,
    trend,
)
