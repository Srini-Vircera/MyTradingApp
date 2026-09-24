"""Helpers to serve recorded-format JSON fixtures through httpx.MockTransport."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx

FIXTURES = Path(__file__).resolve().parents[3] / "fixtures" / "providers"


def load(name: str) -> dict[str, Any]:
    data: dict[str, Any] = json.loads((FIXTURES / name).read_text())
    return data


class Recorder:
    """A MockTransport handler that records requests and routes them to responses."""

    def __init__(self, route: Callable[[httpx.Request], httpx.Response]) -> None:
        self.requests: list[httpx.Request] = []
        self._route = route

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self._route(request)

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self)
