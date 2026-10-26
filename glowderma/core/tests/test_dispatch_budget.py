"""R8: one ``core.dispatch_outbox`` run stops claiming after its wall-clock budget, and a Celery soft time
limit inside a handler fails that message and ends the run (never a run that keeps claiming past it)."""

from __future__ import annotations

import pytest
from celery.exceptions import SoftTimeLimitExceeded  # type: ignore[import-untyped]

from glowderma.core import outbox
from glowderma.core import tasks as core_tasks
from glowderma.core.models import OutboxMessage
from glowderma.core.tasks import dispatch_outbox

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def isolated_registry(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(outbox, "_handlers", {})
    monkeypatch.setattr(outbox, "_dead_handlers", {})


def messages(count: int) -> list[OutboxMessage]:
    return [
        OutboxMessage.objects.create(topic="test.slow", payload={"n": n}, dedupe_key=f"test.slow:{n}")
        for n in range(count)
    ]


def test_a_run_stops_claiming_after_its_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    ticks = iter([0.0, 0.0, 100.0, 200.0, 250.0, 260.0, 270.0])
    monkeypatch.setattr(core_tasks, "_clock", lambda: next(ticks))
    handled: list[int] = []

    @outbox.register("test.slow")
    def handler(message: OutboxMessage) -> None:
        handled.append(message.payload["n"])

    messages(5)
    dispatch_outbox(batch=50)
    assert handled == [0, 1, 2]  # the fourth claim would start after 250 s > 240 s
    assert OutboxMessage.objects.filter(status="pending").count() == 2
    assert core_tasks.DISPATCH_BUDGET_SECONDS <= 240


def test_a_soft_time_limit_fails_the_message_and_ends_the_run() -> None:
    handled: list[int] = []

    @outbox.register("test.slow")
    def handler(message: OutboxMessage) -> None:
        handled.append(message.payload["n"])
        raise SoftTimeLimitExceeded()

    first, _second = messages(2)
    dispatch_outbox(batch=50)
    assert handled == [0]
    first.refresh_from_db()
    assert (first.status, first.attempts) == ("failed", 1)
    assert OutboxMessage.objects.filter(status="pending").count() == 1
