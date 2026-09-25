"""Market data: calendar, canonical bars, providers, validation, storage, synthetic history.

Conventions (see ``docs/DATA.md``):

* A bar's index ``timestamp`` is the bar's **end** time in UTC. A daily bar is
  stamped at its session close, so a point-in-time filter ``timestamp <= as_of``
  can never expose today's daily bar before today's close.
* Bars are stored raw (unadjusted); split- and total-return-adjusted series are
  derived locally from corporate actions so every provider is treated alike.
"""
