# Convenience targets. Run `make help`.
PY ?= .venv/bin/python
UV ?= uv

.PHONY: help install lint format typecheck test cov check db-up db-down db-upgrade validate

help:  ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  %-12s %s\n", $$1, $$2}'

install:  ## Create .venv (Python 3.12) and install with dev tools
	$(UV) venv --python 3.12 .venv
	$(UV) pip install --python .venv -e ".[dev]"

lint:  ## Ruff lint + format check
	$(PY) -m ruff check src tests
	$(PY) -m ruff format --check src tests

format:  ## Auto-format
	$(PY) -m ruff format src tests
	$(PY) -m ruff check --fix src tests

typecheck:  ## mypy --strict
	$(PY) -m mypy

test:  ## Run the test suite
	$(PY) -m pytest

cov:  ## Tests with coverage report
	$(PY) -m pytest --cov --cov-report=term-missing

check: lint typecheck test  ## Everything CI runs

validate:  ## Validate configuration for every environment
	for env in development paper production; do .venv/bin/aq --env $$env config validate || exit 1; done

db-up:  ## Start local PostgreSQL
	docker compose up -d postgres

db-down:  ## Stop local PostgreSQL (data volume is kept)
	docker compose down

db-upgrade:  ## Apply database migrations (needs DATABASE_URL in .env)
	$(PY) -m adaptive_quant.cli db upgrade
