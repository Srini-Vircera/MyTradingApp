"""The worker process: executes control-plane jobs and supervises the scheduler.

It is the only component with the persistent volume (market data, reports,
research registry). Jobs are claimed from PostgreSQL; nothing here executes
shell commands, and research/backtest/data jobs cannot reach a broker.
"""
