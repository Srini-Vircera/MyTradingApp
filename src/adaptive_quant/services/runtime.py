"""The effective configuration: reviewed YAML + audited runtime overlay from PostgreSQL.

Every control-plane consumer (API views, worker jobs, the scheduler at start)
uses :func:`effective_config`, so the whole system agrees on one validated
``config_version``. Strategy overrides are validated with the strategy registry
(parameter schemas and grids) in addition to the configuration schema.
"""

from __future__ import annotations

import hashlib
import json
import threading
from typing import Any

from adaptive_quant.config.loader import LoadedConfig
from adaptive_quant.config.runtime import apply_runtime
from adaptive_quant.persistence.control import ControlRepository
from adaptive_quant.persistence.db import Database
from adaptive_quant.quant.strategies.catalog import StrategyCatalog

_cache: dict[tuple[int, str, int, str], LoadedConfig] = {}
_lock = threading.Lock()


def resolve(
    base: LoadedConfig, overlay: dict[str, Any], overrides: dict[str, Any], revision: int
) -> LoadedConfig:
    loaded = apply_runtime(base, overlay, overrides, revision=revision)
    if loaded is not base:
        StrategyCatalog.from_config(loaded.settings.strategies)
    return loaded


def effective_config(
    base: LoadedConfig, db: Database | None
) -> tuple[LoadedConfig, dict[str, Any]]:
    """(effective config, runtime-config row). Without a database: the YAML config."""
    if db is None:
        return base, {"revision": 0, "overlay": {}, "strategy_overrides": {}}
    row = ControlRepository(db).runtime_config()
    content = json.dumps([row["overlay"], row["strategy_overrides"]], sort_keys=True)
    key = (
        id(base),
        base.config_version,
        int(row["revision"]),
        hashlib.sha256(content.encode()).hexdigest(),
    )
    with _lock:
        cached = _cache.get(key)
    if cached is not None:
        return cached, row
    loaded = resolve(base, row["overlay"], row["strategy_overrides"], int(row["revision"]))
    with _lock:
        if len(_cache) > 32:
            _cache.clear()
        _cache[key] = loaded
    return loaded, row
