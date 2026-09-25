"""Load, merge, validate and fingerprint configuration.

Resolution order (later files override earlier ones; mappings merge
recursively, lists are replaced wholesale)::

    config/base.yaml -> config/risk.yaml -> config/strategies.yaml -> config/<env>.yaml

Only two environment variables influence *non-secret* configuration:
``AQ_ENV`` (which overlay) and ``AQ_CONFIG_DIR`` (where the files are). Every
other setting lives in version-controlled YAML so that each trading decision
can be tied to an exact, reproducible ``config_version``.
"""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from adaptive_quant.config.schema import (
    LIVE_TRADING_ACKNOWLEDGEMENT,
    Settings,
    is_secret_key,
)
from adaptive_quant.config.secrets import Secrets
from adaptive_quant.core.enums import DeploymentEnvironment, TradingMode
from adaptive_quant.core.errors import ConfigurationError, SafetyViolation

SHARED_FILES = ("base.yaml", "risk.yaml", "strategies.yaml")
#: Value ``AQ_LIVE_TRADING_CONFIRM`` must hold (in the deployment environment) for live mode.
LIVE_CONFIRM_ENV_VALUE = "yes-trade-real-money"


@dataclass(frozen=True)
class SourceFile:
    path: Path
    sha256: str


@dataclass(frozen=True)
class LoadedConfig:
    """Validated settings plus provenance.

    ``config_version`` is recorded with every signal, risk decision and order
    so any decision can be traced to the exact configuration that produced it.
    """

    settings: Settings
    config_version: str
    digest: str
    project_root: Path
    sources: tuple[SourceFile, ...]
    warnings: tuple[str, ...] = field(default=())

    def resolve_path(self, path: Path) -> Path:
        """Resolve a configured relative path against the project root."""
        return path if path.is_absolute() else (self.project_root / path).resolve()


def default_config_dir() -> Path:
    env = os.environ.get("AQ_CONFIG_DIR")
    return Path(env) if env else Path.cwd() / "config"


def load_config(
    environment: str | DeploymentEnvironment | None = None,
    *,
    config_dir: Path | None = None,
    secrets: Secrets | None = None,
) -> LoadedConfig:
    """Load and validate configuration for ``environment``.

    Raises :class:`ConfigurationError` for malformed or inconsistent files and
    :class:`SafetyViolation` if the requested trading mode is not authorised.
    """
    env = _parse_environment(environment or os.environ.get("AQ_ENV") or "development")
    cfg_dir = (config_dir or default_config_dir()).resolve()
    if not cfg_dir.is_dir():
        raise ConfigurationError(
            f"configuration directory not found: {cfg_dir}",
            hint="run commands from the repository root or set AQ_CONFIG_DIR",
        )

    merged: dict[str, Any] = {}
    sources: list[SourceFile] = []
    for name in (*SHARED_FILES, f"{env.value}.yaml"):
        path = cfg_dir / name
        data, digest = _read_yaml(path)
        _reject_embedded_secrets(data, path)
        merged = deep_merge(merged, data)
        sources.append(SourceFile(path=path, sha256=digest))

    declared = merged.setdefault("app", {}).setdefault("environment", env.value)
    if declared != env.value:
        raise ConfigurationError(
            f"{env.value}.yaml declares app.environment={declared!r}",
            hint=f"app.environment must match the overlay file name ({env.value!r})",
        )

    settings = _validate(merged)
    warnings = enforce_trading_mode_policy(settings, secrets if secrets is not None else Secrets())
    digest = fingerprint(settings)
    return LoadedConfig(
        settings=settings,
        config_version=f"{env.value}-{digest[:12]}",
        digest=digest,
        project_root=cfg_dir.parent,
        sources=tuple(sources),
        warnings=tuple(warnings),
    )


def enforce_trading_mode_policy(settings: Settings, secrets: Secrets) -> list[str]:
    """Refuse any configuration that would trade real money without every opt-in.

    Live trading requires *all* of:

    1. ``app.environment: production`` (the ``production.yaml`` overlay),
    2. ``trading.mode: live``,
    3. ``trading.live_trading.enabled: true``,
    4. ``trading.live_trading.acknowledgement`` equal to :data:`LIVE_TRADING_ACKNOWLEDGEMENT`,
    5. environment variable ``AQ_LIVE_TRADING_CONFIRM`` equal to :data:`LIVE_CONFIRM_ENV_VALUE`,
    6. a real (non-simulated) broker provider.

    Nothing in the code base ever writes these values; they can only be set by a
    human editing files and the deployment environment. Returns non-fatal warnings.
    """
    warnings: list[str] = []
    trading = settings.trading
    env = settings.app.environment

    if trading.mode is not TradingMode.LIVE:
        if trading.live_trading.enabled:
            warnings.append(
                f"trading.live_trading.enabled is true but mode is {trading.mode}; "
                "live trading stays OFF"
            )
        return warnings

    problems: list[str] = []
    if env is not DeploymentEnvironment.PRODUCTION:
        problems.append(f"environment is {env.value!r}; live mode requires 'production'")
    if not trading.live_trading.enabled:
        problems.append("trading.live_trading.enabled is false")
    if trading.live_trading.acknowledgement != LIVE_TRADING_ACKNOWLEDGEMENT:
        problems.append(
            "trading.live_trading.acknowledgement must be exactly: "
            f"{LIVE_TRADING_ACKNOWLEDGEMENT!r}"
        )
    confirm = secrets.live_trading_confirm
    if confirm is None or confirm.get_secret_value() != LIVE_CONFIRM_ENV_VALUE:
        problems.append(
            f"environment variable AQ_LIVE_TRADING_CONFIRM != {LIVE_CONFIRM_ENV_VALUE!r}"
        )
    if settings.broker.provider == "simulated":
        problems.append("broker.provider 'simulated' cannot trade live")
    if problems:
        raise SafetyViolation(
            "live trading requested but not fully authorised:\n    - " + "\n    - ".join(problems),
            hint="use trading.mode: paper (or shadow) unless you have completed "
            "the live-trading checklist in docs/SAFETY.md",
        )
    return warnings


def deep_merge(base: Mapping[str, Any], override: Mapping[str, Any]) -> dict[str, Any]:
    """Recursively merge mappings; non-mapping values (including lists) are replaced."""
    result: dict[str, Any] = dict(base)
    for key, value in override.items():
        if isinstance(value, Mapping) and isinstance(result.get(key), Mapping):
            result[key] = deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def fingerprint(settings: Settings) -> str:
    """SHA-256 of the canonical JSON form of the resolved settings."""
    canonical = json.dumps(settings.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


# ------------------------------------------------------------------ internals
def _parse_environment(value: str | DeploymentEnvironment) -> DeploymentEnvironment:
    try:
        return DeploymentEnvironment(value)
    except ValueError:
        options = ", ".join(e.value for e in DeploymentEnvironment)
        raise ConfigurationError(
            f"unknown environment {value!r}", hint=f"choose one of: {options}"
        ) from None


def _read_yaml(path: Path) -> tuple[dict[str, Any], str]:
    if not path.is_file():
        raise ConfigurationError(f"missing configuration file: {path}")
    raw = path.read_bytes()
    try:
        data = yaml.safe_load(raw)
    except yaml.YAMLError as exc:
        raise ConfigurationError(f"{path.name} is not valid YAML: {exc}") from exc
    if data is None:
        data = {}
    if not isinstance(data, dict):
        raise ConfigurationError(f"{path.name} must contain a mapping at the top level")
    return data, hashlib.sha256(raw).hexdigest()


def _reject_embedded_secrets(data: Any, path: Path, prefix: str = "") -> None:
    if isinstance(data, dict):
        for key, value in data.items():
            dotted = f"{prefix}.{key}" if prefix else str(key)
            if is_secret_key(str(key)) and value not in (None, ""):
                raise ConfigurationError(
                    f"{path.name}: '{dotted}' looks like a credential",
                    hint="never put secrets in YAML; use environment variables (.env)",
                )
            _reject_embedded_secrets(value, path, dotted)
    elif isinstance(data, list):
        for i, item in enumerate(data):
            _reject_embedded_secrets(item, path, f"{prefix}[{i}]")


def _validate(merged: dict[str, Any]) -> Settings:
    try:
        return Settings.model_validate(merged)
    except ValidationError as exc:
        lines = []
        for err in exc.errors():
            loc = ".".join(str(p) for p in err["loc"]) or "(root)"
            lines.append(f"  - {loc}: {err['msg']}")
        raise ConfigurationError(
            "configuration is invalid:\n" + "\n".join(lines),
            hint="fix the listed keys in config/*.yaml (unknown keys are rejected on purpose)",
        ) from None
