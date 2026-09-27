"""Application services shared by the CLI and the worker's control-plane jobs.

Each function orchestrates existing domain components (data pipeline, backtest
engine, research pipeline) and returns plain, JSON-ready results. Nothing here
talks to a broker or places orders, and nothing executes shell commands.
"""
