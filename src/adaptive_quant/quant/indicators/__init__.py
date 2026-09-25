"""Point-in-time technical indicators.

Every indicator is **causal**: the value at row ``t`` is a function of rows
``<= t`` only. Every indicator is **strict about warm-up**: it is ``NaN`` until
its full window is available - never a partial-window value.

Layout
------
* ``_common``    - input checks and shared rolling helpers
* ``trend``      - SMA, EMA, distance from MA, rolling z-score, trend slope / R²
* ``momentum``   - rate of change, momentum (log), RSI, momentum acceleration
* ``volatility`` - rolling std, historical volatility, ATR, Bollinger Bands,
  Bollinger width / %B, volatility percentile
* ``ranges``     - rolling highs / lows
* ``performance``- drawdown, drawdown duration, rolling Sharpe
* ``specs``      - declarative, validated indicator specs + registry (for config)
* ``engine``     - compute many indicators at once; point-in-time snapshots

Exact definitions and conventions: ``docs/INDICATORS.md``.
"""
