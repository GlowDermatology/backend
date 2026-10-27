"""Dead-letter hooks: the orchestrating module records the business consequence once a message goes dead."""

import logging

import pytest

from glowderma.core import outbox
from glowderma.core.models import AuditEntry, OutboxMessage
from glowderma.core.tasks import dispatch_outbox

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def isolated_registry(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(outbox, "_handlers", {})
    monkeypatch.setattr(outbox, "_dead_handlers", {})


def failing_message() -> OutboxMessage:
    return OutboxMessage.objects.create(
        topic="test.fail", payload={}, dedupe_key="test.fail:1", attempts=9, status="failed"
    )


def test_dead_hook_runs_once_after_the_audit_entry() -> None:
    seen: list[tuple[int, str, int]] = []

    @outbox.register("test.fail")
    def handler(message: OutboxMessage) -> None:
        raise RuntimeError("boom")

    @outbox.on_dead("test.fail")
    def dead(message: OutboxMessage) -> None:
        seen.append((message.pk, message.status, AuditEntry.objects.filter(action="outbox.dead").count()))

    message = failing_message()
    dispatch_outbox(batch=1)
    message.refresh_from_db()
    assert message.status == "dead"
    assert seen == [(message.pk, "dead", 1)]
    dispatch_outbox(batch=1)  # dead rows are never claimed again
    assert len(seen) == 1


def test_dead_hook_errors_are_logged_not_raised(caplog: pytest.LogCaptureFixture) -> None:
    @outbox.register("test.fail")
    def handler(message: OutboxMessage) -> None:
        raise RuntimeError("boom")

    @outbox.on_dead("test.fail")
    def dead(message: OutboxMessage) -> None:
        raise ValueError("hook broke")

    message = failing_message()
    with caplog.at_level(logging.ERROR, logger="glowderma.core.outbox"):
        dispatch_outbox(batch=1)
    message.refresh_from_db()
    assert message.status == "dead"
    assert "Dead-letter hook failed" in caplog.text


def test_failure_before_dead_does_not_notify() -> None:
    calls: list[int] = []

    @outbox.register("test.fail")
    def handler(message: OutboxMessage) -> None:
        raise RuntimeError("boom")

    @outbox.on_dead("test.fail")
    def dead(message: OutboxMessage) -> None:
        calls.append(message.pk)

    OutboxMessage.objects.create(topic="test.fail", payload={}, dedupe_key="test.fail:2")
    dispatch_outbox(batch=1)
    assert calls == []


def test_permanent_failure_goes_dead_at_the_first_attempt() -> None:
    """A handler that raises ``PermanentFailure`` (the provider refused for good) is never retried: the
    message is dead at once with the reason as ``last_error``, audited and handed to the dead hook."""
    seen: list[str] = []

    @outbox.register("test.fail")
    def handler(message: OutboxMessage) -> None:
        raise outbox.PermanentFailure("Billz refused the sale: product not found (20036)")

    @outbox.on_dead("test.fail")
    def dead(message: OutboxMessage) -> None:
        seen.append(message.last_error)

    message = OutboxMessage.objects.create(topic="test.fail", payload={}, dedupe_key="test.fail:3")
    dispatch_outbox(batch=1)
    message.refresh_from_db()
    assert (message.status, message.attempts) == ("dead", 1)
    assert message.last_error == "Billz refused the sale: product not found (20036)"
    entry = AuditEntry.objects.get(action="outbox.dead")
    assert entry.after == {
        "topic": "test.fail",
        "dedupe_key": "test.fail:3",
        "attempts": 1,
        "last_error": "Billz refused the sale: product not found (20036)",
    }
    assert seen == ["Billz refused the sale: product not found (20036)"]
    assert outbox.MAX_ATTEMPTS == 10
