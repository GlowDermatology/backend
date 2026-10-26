from datetime import timedelta
from threading import Event, Thread
from typing import Any
from unittest.mock import patch
from uuid import uuid4

import pytest
from django.db import connection, transaction

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


def test_a_broker_outage_after_commit_never_fails_the_request(
    django_capture_on_commit_callbacks: Any, caplog: pytest.LogCaptureFixture
) -> None:
    """The message is committed before the kick; a broker error is logged and the beat delivers it."""
    with patch.object(dispatch_outbox, "delay", side_effect=OSError("broker down")):
        with django_capture_on_commit_callbacks(execute=True):
            message = enqueue()
    assert OutboxMessage.objects.filter(pk=message.pk, status="pending").exists()
    assert "broker down" in caplog.text


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


@pytest.mark.parametrize("newer_status", ["in_flight", "done"])
@pytest.mark.parametrize("outcome", ["done", "failed", "dead"])
def test_stale_completion_cannot_overwrite_newer_claim(
    newer_status: str, outcome: str, caplog: pytest.LogCaptureFixture
) -> None:
    message = enqueue()
    initial_attempts = 9 if outcome == "dead" else 0
    OutboxMessage.objects.filter(pk=message.pk).update(attempts=initial_attempts)
    newer_token = uuid4()
    newer_lease = timeutils.now() + timedelta(minutes=10)

    @outbox.register("test")
    def finish_after_reclaim(claimed: OutboxMessage) -> None:
        assert claimed.claim_token != newer_token
        OutboxMessage.objects.filter(pk=claimed.pk).update(
            claim_token=newer_token,
            status=newer_status,
            attempts=initial_attempts + 2,
            lease_expires_at=newer_lease,
            last_error="newer worker state",
        )
        claimed.refresh_from_db()
        if outcome != "done":
            raise RuntimeError("stale failure")

    dispatch_outbox(batch=1)
    message.refresh_from_db()
    assert message.status == newer_status
    assert message.claim_token == newer_token
    assert message.attempts == initial_attempts + 2
    assert message.lease_expires_at == newer_lease
    assert message.last_error == "newer worker state"
    assert not AuditEntry.objects.filter(action="outbox.dead").exists()
    assert "stale outbox completion" in caplog.text


def test_each_row_gets_fresh_lease_when_it_starts(monkeypatch: pytest.MonkeyPatch) -> None:
    instant = timeutils.now()
    current = instant
    monkeypatch.setattr(timeutils, "now", lambda: current)
    first = OutboxMessage.objects.create(topic="test", payload={}, dedupe_key="first")
    second = OutboxMessage.objects.create(topic="test", payload={}, dedupe_key="second")
    claims: list[OutboxMessage] = []

    @outbox.register("test")
    def slow_handler(message: OutboxMessage) -> None:
        nonlocal current
        claims.append(message)
        if message.pk == first.pk:
            second.refresh_from_db()
            assert second.status == "pending"
            assert second.claim_token is None
            current = instant + timedelta(minutes=6)

    dispatch_outbox(batch=2)
    assert [message.pk for message in claims] == [first.pk, second.pk]
    assert claims[0].lease_expires_at == instant + timedelta(minutes=5)
    assert claims[1].lease_expires_at == instant + timedelta(minutes=11)
    assert claims[0].claim_token != claims[1].claim_token


@pytest.mark.django_db(transaction=True)
def test_skip_locked_claims_disjoint_rows() -> None:
    locked, release, ready, start_dispatch, finished = (Event() for _ in range(5))
    calls: list[int] = []
    errors: list[BaseException] = []
    first = OutboxMessage.objects.create(topic="test", payload={}, dedupe_key="first")
    second = OutboxMessage.objects.create(topic="test", payload={}, dedupe_key="second")
    outbox.register("test")(lambda message: calls.append(message.pk))

    def hold_row_lock() -> None:
        try:
            with transaction.atomic():
                OutboxMessage.objects.select_for_update().get(pk=first.pk)
                locked.set()
                if not release.wait(timeout=10):
                    raise TimeoutError("row lock was not released")
        except BaseException as exc:
            errors.append(exc)
        finally:
            connection.close()

    def dispatch_other_row() -> None:
        try:
            connection.ensure_connection()
            ready.set()
            if not start_dispatch.wait(timeout=10):
                raise TimeoutError("dispatcher was not started")
            dispatch_outbox(batch=1)
        except BaseException as exc:
            errors.append(exc)
        finally:
            finished.set()
            connection.close()

    locker = Thread(target=hold_row_lock, daemon=True)
    dispatcher = Thread(target=dispatch_other_row, daemon=True)
    locker.start()
    dispatcher.start()
    try:
        assert locked.wait(timeout=10)
        assert ready.wait(timeout=10)
        start_dispatch.set()
        assert finished.wait(timeout=1), "dispatcher blocked on the locked row"
        assert calls == [second.pk]
    finally:
        release.set()
        start_dispatch.set()
        locker.join(timeout=10)
        dispatcher.join(timeout=10)
    assert not locker.is_alive() and not dispatcher.is_alive()
    assert errors == []
    first.refresh_from_db()
    second.refresh_from_db()
    assert first.status == "pending"
    assert second.status == "done"
