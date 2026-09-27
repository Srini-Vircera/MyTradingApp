"""Runtime configuration overlay (control plane, PR #4).

The YAML files remain the reviewed source of truth. The operator may change a
small, explicit set of settings at runtime; those changes live in PostgreSQL
(``runtime_config``) and are applied on top of the YAML here:

* Only paths listed in :data:`SETTINGS` with class ``runtime`` or
  ``runtime_confirm`` can be set. Everything else is refused, whatever the
  request says.
* Risk limits (``runtime_confirm``) may only be **tightened** relative to the
  reviewed YAML value, and need an explicit confirmation.
* ``trading.mode`` may only be ``shadow`` or ``paper``, and only through the
  Trading Control workflow. Live mode, the live-trading flags, broker endpoints,
  the kill switch and every secret are never runtime-editable.
* Strategy overrides may change parameters, the enabled flag and the lifecycle
  (never to ``live_approved``); callers validate them with the strategy registry
  (``services.runtime.effective_config``).
* The merged result is validated with the same :class:`Settings` schema and the
  explicit refusal of anything that could enable live trading, and fingerprinted, so every overlay
  change produces a new ``config_version`` that is recorded with jobs, signals,
  risk decisions and orders.
"""

from __future__ import annotations

import copy
import dataclasses
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from pydantic import ValidationError

from adaptive_quant.config.loader import LoadedConfig, fingerprint
from adaptive_quant.config.schema import Settings
from adaptive_quant.core.enums import StrategyLifecycle, TradingMode
from adaptive_quant.core.errors import ConfigurationError, SafetyViolation


class SettingClass(StrEnum):
    RUNTIME = "runtime"  # safe to change from the UI; applies to the next job
    RUNTIME_CONFIRM = "runtime_confirm"  # UI with confirmation; tighten-only
    RESTART = "restart"  # read at process start: change YAML/deployment and restart
    SECRET = "secret"  # noqa: S105 - a category name: environment/secret store only
    IMMUTABLE = "immutable"  # reviewed repository change only (safety-critical)


class Category(StrEnum):
    DATA = "Data"
    BACKTESTING = "Backtesting"
    RESEARCH = "Research"
    STRATEGIES = "Strategies"
    RISK = "Risk"
    TRADING = "Trading"
    BROKER = "Broker"
    NOTIFICATIONS = "Notifications"
    SYSTEM = "System"


@dataclass(frozen=True)
class SettingSpec:
    path: str
    category: Category
    klass: SettingClass
    label: str
    help: str = ""
    #: runtime_confirm only: "lower" or "higher" is the tighter direction
    tighter: str | None = None
    choices: tuple[str, ...] = ()


R, RC = SettingClass.RUNTIME, SettingClass.RUNTIME_CONFIRM
C = Category

#: Settings the operator may change at runtime (everything else is read-only in the UI).
EDITABLE: tuple[SettingSpec, ...] = (
    SettingSpec("data.primary_provider", C.DATA, R, "Default data provider",
                choices=("file", "alpaca", "polygon")),
    SettingSpec("data.history_start", C.DATA, R, "Default download start date"),
    SettingSpec("backtest.initial_capital", C.BACKTESTING, R, "Starting capital (USD)"),
    SettingSpec("backtest.execution", C.BACKTESTING, R, "Execution timing",
                choices=("near_close", "next_open", "next_close", "closing_auction")),
    SettingSpec("backtest.execution_delay_bars", C.BACKTESTING, R, "Execution delay (sessions)"),
    SettingSpec("backtest.use_synthetic_history", C.BACKTESTING, R,
                "Extend TQQQ/SQQQ with SYNTHETIC history"),
    SettingSpec("backtest.cash_interest_annual", C.BACKTESTING, R, "Interest on cash (annual)"),
    SettingSpec("backtest.sizing_cash_buffer", C.BACKTESTING, R, "Sizing cash buffer"),
    SettingSpec("backtest.costs.commission_per_share", C.BACKTESTING, R, "Commission per share"),
    SettingSpec("backtest.costs.commission_per_order", C.BACKTESTING, R, "Commission per order"),
    SettingSpec("backtest.costs.commission_minimum", C.BACKTESTING, R, "Minimum commission"),
    SettingSpec("backtest.costs.half_spread_bps.default", C.BACKTESTING, R,
                "Half spread (bps, default)"),
    SettingSpec("backtest.costs.slippage_bps", C.BACKTESTING, R, "Slippage (bps)"),
    SettingSpec("backtest.costs.impact_coefficient_bps", C.BACKTESTING, R,
                "Market impact coefficient (bps)"),
    SettingSpec("backtest.costs.max_participation", C.BACKTESTING, R,
                "Max participation of average daily volume"),
    SettingSpec("research.max_grid_points", C.RESEARCH, R, "Max parameter-grid points"),
    SettingSpec("research.workers", C.RESEARCH, R, "Parallel trial processes"),
    SettingSpec("research.monte_carlo.simulations", C.RESEARCH, R, "Monte Carlo resamples"),
    SettingSpec("research.bootstrap.samples", C.RESEARCH, R, "Bootstrap samples"),
    SettingSpec("research.walk_forward.scheme", C.RESEARCH, R, "Walk-forward scheme",
                choices=("rolling", "anchored")),
    SettingSpec("notifications.min_severity", C.NOTIFICATIONS, R, "Minimum alert severity",
                choices=("info", "warning", "error", "critical")),
    # ---- risk limits: tighten-only, with explicit confirmation
    SettingSpec("risk.max_gross_exposure", C.RISK, RC, "Max gross exposure", tighter="lower"),
    SettingSpec("risk.max_net_underlying_exposure", C.RISK, RC, "Max net QQQ-equivalent exposure",
                tighter="lower"),
    SettingSpec("risk.max_inverse_net_exposure", C.RISK, RC, "Max inverse (short) exposure",
                tighter="lower"),
    SettingSpec("risk.max_leveraged_etf_weight", C.RISK, RC, "Max leveraged-ETF weight",
                tighter="lower"),
    SettingSpec("risk.max_daily_turnover", C.RISK, RC, "Max daily turnover", tighter="lower"),
    SettingSpec("risk.max_daily_loss", C.RISK, RC, "Daily loss halt", tighter="lower"),
    SettingSpec("risk.max_order_notional", C.RISK, RC, "Max order notional (USD)",
                tighter="lower"),
    SettingSpec("risk.min_cash_weight", C.RISK, RC, "Minimum cash weight", tighter="higher"),
    SettingSpec("risk.volatility_target.annualized_target", C.RISK, RC,
                "Volatility target (annual)", tighter="lower"),
    SettingSpec("risk.volatility_target.max_scale", C.RISK, RC, "Volatility max scale",
                tighter="lower"),
)  # fmt: skip

#: Only the Trading Control workflow may write this path, and only these values.
TRADING_MODE_PATH = "trading.mode"
RUNTIME_MODES = (TradingMode.SHADOW.value, TradingMode.PAPER.value)

_BY_PATH = {s.path: s for s in EDITABLE}

#: read-only classification by path prefix (first match wins)
_PREFIX_RULES: tuple[tuple[str, Category, SettingClass, str], ...] = (
    ("trading.mode", C.TRADING, SettingClass.IMMUTABLE,
     "Trading Control page (shadow/paper only); live mode is never available from the UI"),
    ("trading.live_trading", C.TRADING, SettingClass.IMMUTABLE,
     "live-trading lock: reviewed repository change + deployment secret only"),
    ("trading.kill_switch", C.TRADING, SettingClass.IMMUTABLE,
     "use the kill switch controls; storage is fixed by deployment"),
    ("trading.", C.TRADING, SettingClass.IMMUTABLE, "trading safety parameters"),
    ("broker.", C.BROKER, SettingClass.IMMUTABLE,
     "broker endpoints are fixed (Alpaca paper); credentials are deployment secrets"),
    ("risk.drawdown_bands", C.RISK, SettingClass.IMMUTABLE, "reviewed repository change"),
    ("risk.", C.RISK, SettingClass.IMMUTABLE, "reviewed repository change"),
    ("research.gates", C.RESEARCH, SettingClass.IMMUTABLE,
     "promotion gates: reviewed repository change only"),
    ("research.", C.RESEARCH, SettingClass.RESTART, "research output paths and methodology"),
    ("backtest.", C.BACKTESTING, SettingClass.RESTART, "engine structure and paths"),
    ("data.staleness", C.DATA, SettingClass.IMMUTABLE, "data-freshness safety limits"),
    ("data.", C.DATA, SettingClass.RESTART, "storage paths and provider endpoints"),
    ("strategies.", C.STRATEGIES, SettingClass.IMMUTABLE, "use the Strategy Manager"),
    ("universe.", C.TRADING, SettingClass.IMMUTABLE, "tradeable universe"),
    ("schedule.", C.TRADING, SettingClass.RESTART, "trading-cycle schedule"),
    ("notifications.", C.NOTIFICATIONS, SettingClass.RESTART, "delivery settings"),
    ("api.", C.SYSTEM, SettingClass.RESTART, "deployment setting"),
    ("app.", C.SYSTEM, SettingClass.IMMUTABLE, "deployment environment"),
    ("", C.SYSTEM, SettingClass.RESTART, "read at start-up"),
)  # fmt: skip

#: credentials, always environment/secret-store managed and never displayed
SECRET_SETTINGS: tuple[tuple[str, Category, str], ...] = (
    ("ALPACA_API_KEY_ID", C.BROKER, "Alpaca paper key id"),
    ("ALPACA_API_SECRET_KEY", C.BROKER, "Alpaca paper secret key"),
    ("POLYGON_API_KEY", C.DATA, "Polygon API key"),
    ("DATABASE_URL", C.SYSTEM, "PostgreSQL connection"),
    ("SMTP_USERNAME", C.NOTIFICATIONS, "SMTP user"),
    ("SMTP_PASSWORD", C.NOTIFICATIONS, "SMTP password"),
    ("AQ_API_TOKEN", C.SYSTEM, "Operator API token"),
    ("AQ_LIVE_TRADING_CONFIRM", C.TRADING, "Live-trading confirmation (must stay unset)"),
)


def classify(path: str) -> tuple[Category, SettingClass, str]:
    spec = _BY_PATH.get(path)
    if spec is not None:
        return spec.category, spec.klass, spec.help
    for prefix, cat, klass, why in _PREFIX_RULES:
        if path == prefix or path.startswith(prefix):
            return cat, klass, why
    return C.SYSTEM, SettingClass.RESTART, "read at start-up"  # pragma: no cover


def get_path(data: Any, path: str) -> Any:
    cur = data
    for part in path.split("."):
        if not isinstance(cur, dict) or part not in cur:
            raise KeyError(path)
        cur = cur[part]
    return cur


def _set_path(data: dict[str, Any], path: str, value: Any) -> None:
    parts = path.split(".")
    cur = data
    for part in parts[:-1]:
        nxt = cur.get(part)
        if not isinstance(nxt, dict):
            raise KeyError(path)
        cur = nxt
    if parts[-1] not in cur:
        raise KeyError(path)
    cur[parts[-1]] = value


# ================================================================== strategy overrides
STRATEGY_FIELDS = ("params", "enabled", "lifecycle")
#: lifecycle states the UI may set (LIVE_APPROVED stays a reviewed repository change)
UI_LIFECYCLES = tuple(
    s.value for s in StrategyLifecycle if s is not StrategyLifecycle.LIVE_APPROVED
)


def validate_overlay(overlay: dict[str, Any]) -> None:
    for path, value in overlay.items():
        if path == TRADING_MODE_PATH:
            if value not in RUNTIME_MODES:
                raise SafetyViolation(
                    f"trading.mode may only be set to {' or '.join(RUNTIME_MODES)} at runtime",
                    hint="live mode needs the reviewed checklist in docs/SAFETY.md",
                )
            continue
        spec = _BY_PATH.get(path)
        if spec is None:
            raise ConfigurationError(
                f"{path} cannot be changed at runtime",
                hint="it is read at start-up, safety-critical or a secret; "
                "change it through a reviewed configuration change",
            )


def validate_strategy_overrides(overrides: dict[str, Any]) -> None:
    for sid, ov in overrides.items():
        if not isinstance(ov, dict) or set(ov) - set(STRATEGY_FIELDS):
            raise ConfigurationError(f"invalid override for strategy {sid}")
        lc = ov.get("lifecycle")
        if lc is not None and lc not in UI_LIFECYCLES:
            raise SafetyViolation(
                f"{sid}: lifecycle {lc!r} cannot be set from the control plane",
                hint="live_approved requires a reviewed change to strategies.yaml with a "
                "written approval block",
            )


def check_tightening(base: Settings, path: str, value: Any) -> None:
    """Risk limits may only move in the tighter direction from the reviewed YAML value."""
    spec = _BY_PATH[path]
    if spec.tighter is None:
        return
    reviewed = get_path(base.model_dump(mode="json"), path)
    try:
        new, old = float(value), float(reviewed)
    except (TypeError, ValueError) as exc:
        raise ConfigurationError(f"{path} must be a number") from exc
    looser = new > old if spec.tighter == "lower" else new < old
    if looser:
        raise SafetyViolation(
            f"{path}: {new:g} would loosen the reviewed limit {old:g}",
            hint="risk limits can only be tightened from the UI; loosening needs a "
            "reviewed change to config/risk.yaml",
        )


def apply_runtime(
    base: LoadedConfig,
    overlay: dict[str, Any] | None,
    strategy_overrides: dict[str, Any] | None = None,
    *,
    revision: int = 0,
) -> LoadedConfig:
    """YAML + runtime overlay, re-validated and re-fingerprinted.

    An empty overlay returns ``base`` unchanged (same config_version).
    """
    overlay = overlay or {}
    strategy_overrides = strategy_overrides or {}
    if not overlay and not strategy_overrides:
        return base
    validate_overlay(overlay)
    validate_strategy_overrides(strategy_overrides)
    merged = copy.deepcopy(base.settings.model_dump(mode="json"))
    for path, value in overlay.items():
        spec = _BY_PATH.get(path)
        if spec is not None and spec.klass is SettingClass.RUNTIME_CONFIRM:
            check_tightening(base.settings, path, value)
        try:
            _set_path(merged, path, value)
        except KeyError:
            raise ConfigurationError(f"unknown setting {path}") from None
    entries = merged["strategies"]["strategies"]
    known = {e["id"] for e in entries}
    unknown = set(strategy_overrides) - known
    if unknown:
        raise ConfigurationError(f"overrides for unknown strategies: {sorted(unknown)}")
    for e in entries:
        ov = strategy_overrides.get(e["id"])
        if not ov:
            continue
        if "params" in ov:
            e["params"] = {**e.get("params", {}), **ov["params"]}
        if "enabled" in ov:
            e["enabled"] = bool(ov["enabled"])
        if "lifecycle" in ov:
            e["lifecycle"] = ov["lifecycle"]
    try:
        settings = Settings.model_validate(merged)
    except ValidationError as exc:
        lines = [f"{'.'.join(map(str, e['loc']))}: {e['msg']}" for e in exc.errors()]
        raise ConfigurationError("runtime configuration is invalid: " + "; ".join(lines)) from None
    # the overlay can never enable live mode (live_trading.* is not editable either);
    # enforce_trading_mode_policy therefore has nothing to authorise here
    if settings.trading.mode.uses_real_money or settings.trading.live_trading.enabled:
        raise SafetyViolation("the runtime overlay may never enable live trading")
    warnings: list[str] = []
    digest = fingerprint(settings)
    return dataclasses.replace(
        base,
        settings=settings,
        config_version=f"{settings.app.environment.value}-{digest[:12]}",
        digest=digest,
        warnings=(
            *base.warnings,
            *warnings,
            f"runtime overlay revision {revision} applied on top of the YAML configuration",
        ),
    )


def settings_catalog(base: Settings, effective: Settings) -> list[dict[str, Any]]:
    """Every leaf setting with category, class and value (secrets are never included)."""
    from adaptive_quant.config.schema import redact

    base_d = redact(base.model_dump(mode="json"))
    eff_d = redact(effective.model_dump(mode="json"))
    rows: list[dict[str, Any]] = []

    def walk(node: Any, prefix: str) -> None:
        if isinstance(node, dict) and node and prefix != "strategies.strategies":
            for k, v in node.items():
                walk(v, f"{prefix}.{k}" if prefix else str(k))
            return
        if prefix.startswith("strategies.strategies"):
            return
        cat, klass, why = classify(prefix)
        spec = _BY_PATH.get(prefix)
        try:
            reviewed = get_path(base_d, prefix)
        except KeyError:
            reviewed = None
        rows.append(
            {
                "path": prefix,
                "category": cat.value,
                "class": klass.value,
                "label": spec.label if spec else prefix.split(".")[-1].replace("_", " "),
                "value": node,
                "reviewed_value": reviewed,
                "overridden": node != reviewed,
                "tighter": spec.tighter if spec else None,
                "choices": list(spec.choices) if spec else [],
                "why": why,
            }
        )

    walk(eff_d, "")
    return rows
