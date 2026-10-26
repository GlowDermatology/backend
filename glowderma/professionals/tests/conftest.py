from collections.abc import Callable
from datetime import datetime
from typing import Any

import pytest

from glowderma.core import seeding, timeutils

# The design anchor: Saturday 24 Oct 2026 12:05 Tashkent. Staff tokens are issued at the real time and the
# session window runs from there, so a pinned instant in the past keeps them valid.
ANCHOR = datetime.fromisoformat(seeding.DEFAULT_ANCHOR)


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest) -> Callable[[datetime], None]:
    """Pin ``timeutils.now()`` to :data:`ANCHOR`; call the fixture's value to move it.

    The test's API clients are built first: their access tokens carry the real ``iat`` / ``exp`` (PyJWT
    checks them against the real clock).
    """
    for name in ("staff_client", "customer_client"):
        if name in request.fixturenames:
            request.getfixturevalue(name)
    state = {"now": ANCHOR}
    monkeypatch.setattr(timeutils, "now", lambda: state["now"])

    def move(instant: datetime) -> None:
        state["now"] = instant

    return move


def errors(response: Any) -> list[tuple[str | None, str, str]]:
    """``[(field, code, message)]`` of a 400 ``validation_error`` envelope."""
    body = response.json()
    assert response.status_code == 400, body
    assert body["code"] == "validation_error" and body["request_id"], body
    return [(item["field"], item["code"], item["message"]) for item in body["errors"]]
