"""Research side: data, indicators, strategies, backtesting, analytics.

Nothing under ``quant`` may import from ``trading`` (brokers, orders) or talk
to a broker; it produces signals and target portfolios only.
"""
