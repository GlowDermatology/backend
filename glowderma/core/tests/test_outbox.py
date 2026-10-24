from datetime import timedelta
from threading import Event, Thread
from typing import Any
from unittest.mock import patch

import pytest
from django.db import connection

from glowderma.core import outbox, timeutils
from glowderma.core.models import AuditEntry, OutboxMessage
from glowderma.core.tasks import dispatch_outbox

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def clean_handlers(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(outbox, "_handlers", {})


def enqueue() -> OutboxMessage:
    return outbox.enqueue("test", {"value": 1}, dedupe_key="test-key")


def test_enqueue_dedupes(django_capture_on_commit_callbacks: Any) -> None:
    with patch.object(dispatch_outbox, "delay") as delay:
        with django_capture_on_commit_callbacks(execute=True):
            first = enqueue()
            second = enqueue()
        assert first.pk == second.pk
        assert OutboxMessage.objects.count() == 1
        assert delay.call_count >= 1


def test_dispatch_runs_handler_and_marks_done() -> None:
    calls: list[int] = []
    outbox.register("test")(lambda message: calls.append(message.pk))
    message = enqueue()
    dispatch_outbox()
    message.refresh_from_db()
    assert message.status == "done"
    assert calls == [message.pk]


def test_failure_backs_off_then_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    instant = timeutils.now()
    monkeypatch.setattr(timeutils, "now", lambda: instant)

    @outbox.register("test")
    def failing(message: OutboxMessage) -> None:
        raise RuntimeError("provider unavailable")

    message = enqueue()
    dispatch_outbox()
    message.refresh_from_db()
    assert message.status == "failed"
    assert message.attempts == 1
    assert message.available_at == instant + timedelta(seconds=2)
    assert message.last_error == "provider unavailable"
    outbox.register("test")(lambda message: None)
    monkeypatch.setattr(timeutils, "now", lambda: instant + timedelta(seconds=3))
    dispatch_outbox()
    message.refresh_from_db()
    assert message.status == "done"


def test_dead_after_max_attempts() -> None:
    @outbox.register("test")
    def failing(message: OutboxMessage) -> None:
        raise RuntimeError("provider unavailable")

    message = enqueue()
    OutboxMessage.objects.filter(pk=message.pk).update(attempts=9)
    dispatch_outbox()
    message.refresh_from_db()
    assert message.status == "dead"
    assert message.attempts == 10
    assert AuditEntry.objects.get(action="outbox.dead").actor_type == "system"


def test_expired_lease_is_reclaimed_and_counted() -> None:
    applied: set[str] = set()
    effects: list[str] = []

    @outbox.register("test")
    def guarded(message: OutboxMessage) -> None:
        if message.dedupe_key not in applied:
            effects.append(message.dedupe_key)
            applied.add(message.dedupe_key)

    message = enqueue()
    guarded(message)  # External success before a simulated worker crash.
    OutboxMessage.objects.filter(pk=message.pk).update(
        status="in_flight", lease_expires_at=timeutils.now() - timedelta(seconds=1)
    )
    dispatch_outbox()
    message.refresh_from_db()
    assert message.status == "done"
    assert message.attempts == 1
    assert effects == ["test-key"]


@pytest.mark.django_db(transaction=True)
def test_skip_locked_claims_disjoint_rows() -> None:
    entered, release = Event(), Event()
    calls: list[int] = []
    errors: list[BaseException] = []
    for index in range(2):
        OutboxMessage.objects.create(topic="test", payload={}, dedupe_key=f"message-{index}")

    @outbox.register("test")
    def handler(message: OutboxMessage) -> None:
        calls.append(message.pk)
        if len(calls) == 1:
            entered.set()
            if not release.wait(timeout=10):
                raise TimeoutError("dispatcher was not released")

    def first_dispatch() -> None:
        try:
            dispatch_outbox(batch=1)
        except BaseException as exc:
            errors.append(exc)
        finally:
            connection.close()

    worker = Thread(target=first_dispatch, daemon=True)
    worker.start()
    try:
        assert entered.wait(timeout=10)
        dispatch_outbox(batch=1)
    finally:
        release.set()
        worker.join(timeout=10)
    assert not worker.is_alive()
    assert errors == []
    assert len(calls) == len(set(calls)) == 2
    assert OutboxMessage.objects.filter(status="done").count() == 2
