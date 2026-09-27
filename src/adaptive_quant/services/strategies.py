"""Strategy management for the control plane (built on the catalogue and governance).

* Parameters are edited against the implementation's typed :class:`ParamSpec`
  schema (never code); a parameter change creates a new strategy *version*, so it
  is only allowed while the strategy is in RESEARCH or DISABLED - evidence and
  approvals attach to versions and must not silently carry over.
* ``enabled`` is a research/configuration switch. *Eligibility* for a trading
  mode additionally depends on the lifecycle (``ELIGIBLE_LIFECYCLES``).
* Lifecycle changes go through :func:`governance.lifecycle.transition` with a
  named HUMAN actor and a written justification; LIVE_APPROVED is never granted
  here (it stays a reviewed repository change with an approval block).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from adaptive_quant.config.loader import LoadedConfig
from adaptive_quant.config.runtime import UI_LIFECYCLES
from adaptive_quant.core.enums import StrategyLifecycle, TradingMode
from adaptive_quant.core.errors import GovernanceError
from adaptive_quant.governance.lifecycle import (
    ALLOWED,
    Actor,
    ActorKind,
    LifecycleTransition,
    is_promotion,
    transition,
)
from adaptive_quant.quant.strategies.catalog import ELIGIBLE_LIFECYCLES, StrategyCatalog
from adaptive_quant.quant.strategies.registry import registry

L = StrategyLifecycle
PARAM_EDITABLE = (L.RESEARCH, L.DISABLED)


def param_schema(implementation: str) -> list[dict[str, Any]]:
    cls = registry()[implementation]
    return [
        {
            "name": s.name,
            "type": s.kind.__name__,
            "default": s.default,
            "minimum": s.minimum,
            "maximum": s.maximum,
            "choices": None if s.choices is None else list(s.choices),
            "description": s.description,
        }
        for s in cls.all_param_specs()
    ]


def strategy_rows(effective: LoadedConfig, base: LoadedConfig) -> list[dict[str, Any]]:
    catalog = StrategyCatalog.from_config(effective.settings.strategies)
    reviewed = {e.id: e for e in base.settings.strategies.strategies}
    rows = []
    for e in catalog.entries:
        v = e.version
        yaml_entry = reviewed.get(v.strategy_id)
        eligible = sorted(m.value for m, ok in ELIGIBLE_LIFECYCLES.items() if v.lifecycle in ok)
        allowed = sorted(t.value for t in ALLOWED[v.lifecycle] if t.value in UI_LIFECYCLES)
        rows.append(
            {
                "strategy_id": v.strategy_id,
                "family": v.family.value,
                "implementation": v.implementation,
                "title": registry()[v.implementation].title or v.strategy_id,
                "description": registry()[v.implementation].description,
                "summary": registry()[v.implementation].summary,
                "code_version": v.code_version,
                "version_id": v.version_id,
                "params": dict(v.params),
                "param_grid": v.param_grid,
                "param_schema": param_schema(v.implementation),
                "warmup_bars": v.warmup_bars,
                "enabled": v.enabled,
                "lifecycle": v.lifecycle.value,
                "eligible_modes": eligible if v.enabled else [],
                "eligible_for_paper_or_shadow": v.enabled
                and v.lifecycle in ELIGIBLE_LIFECYCLES[TradingMode.PAPER],
                "approval": None if v.approval is None else v.approval.model_dump(mode="json"),
                "notes": v.notes,
                "params_editable": v.lifecycle in PARAM_EDITABLE,
                "allowed_transitions": [
                    {
                        "to": t,
                        "promotion": is_promotion(v.lifecycle, L(t)),
                    }
                    for t in allowed
                ],
                "reviewed": None
                if yaml_entry is None
                else {
                    "lifecycle": yaml_entry.lifecycle.value,
                    "enabled": yaml_entry.enabled,
                    "params": dict(yaml_entry.params),
                },
            }
        )
    return rows


def _current(effective: LoadedConfig, sid: str) -> StrategyLifecycle:
    for e in effective.settings.strategies.strategies:
        if e.id == sid:
            return e.lifecycle
    raise GovernanceError(f"unknown strategy {sid!r}")


def with_params(
    effective: LoadedConfig, overrides: dict[str, Any], sid: str, params: dict[str, Any]
) -> dict[str, Any]:
    lifecycle = _current(effective, sid)
    if lifecycle not in PARAM_EDITABLE:
        raise GovernanceError(
            f"{sid} is {lifecycle.value}: parameters can only change in research or disabled",
            hint="a parameter change creates a new version without evidence; demote the "
            "strategy to research first (this is recorded)",
        )
    out = {k: dict(v) for k, v in overrides.items()}
    cur = out.setdefault(sid, {})
    cur["params"] = {**cur.get("params", {}), **params}
    return out


def with_enabled(overrides: dict[str, Any], sid: str, enabled: bool) -> dict[str, Any]:
    out = {k: dict(v) for k, v in overrides.items()}
    out.setdefault(sid, {})["enabled"] = bool(enabled)
    return out


def with_lifecycle(
    effective: LoadedConfig,
    overrides: dict[str, Any],
    sid: str,
    target: str,
    actor: str,
    reason: str,
    now: datetime,
) -> tuple[dict[str, Any], LifecycleTransition]:
    if target not in UI_LIFECYCLES:
        raise GovernanceError(
            f"{target!r} cannot be granted from the control plane",
            hint="live_approved requires a reviewed change to strategies.yaml "
            "with a written approval block",
        )
    current = _current(effective, sid)
    version = next(
        e.version.version_id
        for e in StrategyCatalog.from_config(effective.settings.strategies).entries
        if e.version.strategy_id == sid
    )
    record = transition(
        strategy_id=sid,
        strategy_version=version,
        current=current,
        target=L(target),
        actor=Actor(actor, ActorKind.HUMAN),
        reason=reason,
        at=now,
    )
    out = {k: dict(v) for k, v in overrides.items()}
    out.setdefault(sid, {})["lifecycle"] = target
    return out, record
