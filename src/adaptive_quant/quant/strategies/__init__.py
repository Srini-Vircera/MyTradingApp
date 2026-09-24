"""Candidate trading strategies.

A strategy turns point-in-time market data into a :class:`StrategySignal`
(continuous score in [-1, +1], confidence, suggested exposure, reason and the
indicator values it used). Strategies never size orders, never talk to a
broker and never read credentials: the ensemble, risk engine and order
manager (later milestones) own those decisions.

Every strategy here is a *hypothesis*. None is claimed to have an edge; the
backtesting and walk-forward research of Milestones 5-6 must evaluate them.
See ``docs/STRATEGIES.md``.
"""
