"""Recorded provider answers for the live-adapter tests, served through ``httpx.MockTransport`` — no test ever
reaches the network.

The JSON files under ``fixtures/<provider>/`` are the answers the providers' public documentation shows (see
the ``_source`` keys and ``docs/integrations/<provider>.md``), adapted to our ids; none was recorded from a
live sandbox (no credentials exist yet).
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx

FIXTURES = Path(__file__).resolve().parent / "fixtures"

type Responder = httpx.Response | Callable[[httpx.Request], httpx.Response]


def load(provider: str, name: str) -> Any:
    return json.loads((FIXTURES / provider / f"{name}.json").read_text(encoding="utf-8"))


def reply(provider: str, name: str, status: int = 200) -> httpx.Response:
    return httpx.Response(status, json=load(provider, name))


def connect_error(request: httpx.Request) -> httpx.Response:
    raise httpx.ConnectError("connection refused", request=request)


def read_timeout(request: httpx.Request) -> httpx.Response:
    raise httpx.ReadTimeout("timed out", request=request)


class Script:
    """A scripted provider: each ``(method, path)`` answers its responders in order (the last one repeats);
    every request is recorded. An unscripted route answers 599 so a test never passes by accident."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.routes: dict[tuple[str, str], list[Responder]] = {}
        self.calls: dict[tuple[str, str], int] = {}

    def on(self, method: str, path: str, *responders: Responder) -> Script:
        self.routes[(method, path)] = list(responders)
        return self

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        key = (request.method, request.url.path)
        responders = self.routes.get(key)
        if not responders:
            return httpx.Response(599, json={"error": f"unscripted {key}"})
        index = self.calls.get(key, 0)
        self.calls[key] = index + 1
        responder = responders[min(index, len(responders) - 1)]
        if callable(responder):
            return responder(request)
        # a fresh copy each time: a Response object is consumed by the client that reads it
        return httpx.Response(responder.status_code, headers=responder.headers, content=responder.content)

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self)

    def to(self, method: str, path: str) -> list[httpx.Request]:
        return [r for r in self.requests if r.method == method and r.url.path == path]
