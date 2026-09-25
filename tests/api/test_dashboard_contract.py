"""The dashboard's generated API types must cover the committed OpenAPI schema.

(The dashboard's own CI job regenerates the types and diffs them exactly; this
is a cheap cross-check from the Python side so an API change cannot land
without the dashboard seeing it.)
"""

from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_dashboard_types_cover_every_api_path() -> None:
    schema = json.loads((ROOT / "apps/api/openapi.json").read_text(encoding="utf-8"))
    types = (ROOT / "apps/dashboard/src/lib/api-types.ts").read_text(encoding="utf-8")
    missing = [p for p in schema["paths"] if f'"{p}"' not in types]
    assert missing == [], f"regenerate dashboard types (npm run gen:api): {missing}"


def test_dashboard_writes_only_kill_switch() -> None:
    api = (ROOT / "apps/dashboard/src/lib/api.ts").read_text(encoding="utf-8")
    assert api.count('request(token, "POST"') == 1
    assert "/api/v1/kill-switch/${action}" in api
    for verb in ('"PUT"', '"PATCH"', '"DELETE"'):
        assert verb not in api
