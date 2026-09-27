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


def test_dashboard_writes_exactly_the_api_allowlist() -> None:
    """The dashboard's MUTATIONS list + the upload and kill-switch calls = the API allowlist."""
    import re

    from adaptive_quant.api.app import MUTATING_ROUTES

    api = (ROOT / "apps/dashboard/src/lib/api.ts").read_text(encoding="utf-8")
    block = api[api.index("export const MUTATIONS = [") : api.index("] as const satisfies")]
    listed = set(re.findall(r'"(/api/v1/[^"]+)"', block))
    client = listed | {
        "/api/v1/data/uploads",
        "/api/v1/kill-switch/engage",
        "/api/v1/kill-switch/release",
    }
    assert {("POST", p) for p in client} == set(MUTATING_ROUTES)
    assert api.count('request(token, "POST"') == 3  # killSwitch, mutate, uploadCsv
    assert "/api/v1/kill-switch/${action}" in api
    for verb in ('"PUT"', '"PATCH"', '"DELETE"'):
        assert verb not in api
