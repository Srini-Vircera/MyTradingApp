"""The closed catalogue of worker jobs and their strictly validated parameters.

A job is one of the types below - never a shell command. Parameters are Pydantic
models with ``extra="forbid"``, bounded values and enumerations; they never
contain credentials (providers read secrets from the worker's environment).
"""

from __future__ import annotations

from datetime import date
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

Symbol = Annotated[str, Field(pattern=r"^[A-Z][A-Z0-9.]{0,9}$")]
StrategyId = Annotated[str, Field(pattern=r"^[a-z0-9_]{1,64}$")]
Provider = Literal["file", "alpaca", "polygon"]
Source = Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]{0,31}$")]
FrequencyValue = Literal["1d", "1min", "5min", "15min", "30min"]


class JobParams(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


def _check_range(start: date | None, end: date | None) -> None:
    if start is not None and end is not None and end < start:
        raise ValueError("end date is before start date")
    if start is not None and start < date(1990, 1, 1):
        raise ValueError("start date is before 1990")


class DataDownloadParams(JobParams):
    provider: Provider | None = None
    symbols: list[Symbol] | None = Field(default=None, min_length=1, max_length=20)
    frequency: FrequencyValue = "1d"
    start: date | None = None
    end: date | None = None

    @model_validator(mode="after")
    def _range(self) -> DataDownloadParams:
        _check_range(self.start, self.end)
        return self


class DataValidateParams(JobParams):
    source: Source | None = None
    symbols: list[Symbol] | None = Field(default=None, min_length=1, max_length=20)
    frequency: FrequencyValue = "1d"
    require_fresh: bool = False


class DataSynthesizeParams(JobParams):
    source: Source | None = None
    symbols: list[Literal["TQQQ", "SQQQ"]] | None = Field(default=None, min_length=1)


class DataInventoryParams(JobParams):
    pass


class DataImportParams(JobParams):
    upload_id: Annotated[str, Field(pattern=r"^[0-9a-f]{32}$")]


CostOverrides = dict[
    Literal[
        "commission_per_share",
        "commission_per_order",
        "commission_minimum",
        "slippage_bps",
        "impact_coefficient_bps",
        "max_participation",
    ],
    Annotated[float, Field(ge=0.0, le=5000.0)],
]


ParamName = Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")]
ParamText = Annotated[str, Field(pattern=r"^[A-Za-z0-9_.-]{0,40}$")]
#: per-run strategy parameter overrides: scalars only, validated by the strategy registry
StrategyParams = dict[
    StrategyId, Annotated[dict[ParamName, bool | int | float | ParamText], Field(max_length=20)]
]


class BacktestParams(JobParams):
    strategies: list[StrategyId] = Field(min_length=1, max_length=10)
    #: ensemble = ONE combined portfolio (the long-standing behaviour); independent =
    #: one backtest per strategy over the identical period, compared side by side
    run_mode: Literal["ensemble", "independent"] = "ensemble"
    strategy_params: StrategyParams = Field(default_factory=dict, max_length=10)
    source: Source | None = None
    start: date | None = None
    end: date | None = None
    initial_capital: Annotated[float, Field(gt=0.0, le=1e9)] | None = None
    execution: Literal["near_close", "next_open", "next_close", "closing_auction"] | None = None
    execution_delay_bars: Annotated[int, Field(ge=0, le=20)] | None = None
    use_synthetic_history: bool | None = None
    costs: CostOverrides = Field(default_factory=dict)

    @model_validator(mode="after")
    def _range(self) -> BacktestParams:
        _check_range(self.start, self.end)
        stray = sorted(set(self.strategy_params) - set(self.strategies))
        if stray:
            raise ValueError(f"parameters given for strategies not selected: {stray}")
        return self


class ResearchParams(JobParams):
    strategies: list[StrategyId] | None = Field(default=None, min_length=1, max_length=25)
    source: Source | None = None
    start: date | None = None
    end: date | None = None
    use_synthetic_history: bool | None = None
    simulations: Annotated[int, Field(ge=10, le=100_000)] | None = None
    scheme: Literal["rolling", "anchored"] | None = None

    @model_validator(mode="after")
    def _range(self) -> ResearchParams:
        _check_range(self.start, self.end)
        return self


class BrokerVerifyParams(JobParams):
    """Read-only: verify the Alpaca paper account and run the read-only pre-flight checks."""


JOB_TYPES: dict[str, type[JobParams]] = {
    "data.download": DataDownloadParams,
    "data.validate": DataValidateParams,
    "data.synthesize": DataSynthesizeParams,
    "data.inventory": DataInventoryParams,
    "data.import_upload": DataImportParams,
    "backtest.run": BacktestParams,
    "research.run": ResearchParams,
    "broker.verify": BrokerVerifyParams,
}

#: jobs that may be retried from the UI: re-running them cannot duplicate an effect
#: (the data pipeline merges and versions; results are new, separate rows)
IDEMPOTENT = frozenset(
    {
        "data.download",
        "data.validate",
        "data.synthesize",
        "data.inventory",
        "backtest.run",
        "research.run",
        "broker.verify",
    }
)

TITLES = {
    "data.download": "Download market data",
    "data.validate": "Validate stored data",
    "data.synthesize": "Build SYNTHETIC leveraged-ETF history",
    "data.inventory": "Refresh dataset list",
    "data.import_upload": "Import uploaded CSV",
    "backtest.run": "Backtest",
    "research.run": "Research run",
    "broker.verify": "Verify Alpaca paper account and readiness (read-only)",
}


def parse(job_type: str, params: dict[str, object]) -> JobParams:
    if job_type not in JOB_TYPES:
        raise ValueError(f"unknown job type {job_type!r}")
    return JOB_TYPES[job_type].model_validate(params)
