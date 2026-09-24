"""Adaptive Quant: a risk-first systematic trading platform.

The package is split into layers that only depend "downwards":

* ``core``          - domain types, enums, errors, clocks, money helpers (no I/O).
* ``config``        - typed, validated configuration and secrets loading.
* ``observability`` - structured logging.
* ``governance``    - strategy lifecycle / approval rules.
* ``trading``       - safety gates, order state machine, broker contracts.
* ``notifications`` - alert contracts.

Research modules (data, indicators, strategies, backtest, ...) are added in
later milestones; see ``docs/ROADMAP.md``.
"""

__version__ = "0.1.0"
