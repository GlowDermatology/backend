"""``outbox.RetryLater`` (decision D2) and the sanitised ``last_error`` (review B13) of the dispatcher."""

import logging
from datetime import timedelta

import pytest

from glowderma.core import outbox, timeutils
from glowderma.core.models import AuditEntry, OutboxMessage
from glowderma.core.pii import MASKED_NUMBER, clean_text, scrub_free_text
from glowderma.core.tasks import dispatch_outbox

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def isolated_registry(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(outbox, "_handlers", {})
    monkeypatch.setattr(outbox, "_dead_handlers", {})


def message(**fields: object) -> OutboxMessage:
    return OutboxMessage.objects.create(topic="test.defer", payload={}, dedupe_key="test.defer:1", **fields)


def run_at(monkeypatch: pytest.MonkeyPatch, instant: object) -> None:
    monkeypatch.setattr(timeutils, "now", lambda: instant)
    dispatch_outbox(batch=1)


def test_retry_later_reschedules_without_counting_an_attempt(monkeypatch: pytest.MonkeyPatch) -> None:
    @outbox.register("test.defer")
    def handler(_message: OutboxMessage) -> None:
        raise outbox.RetryLater(timedelta(minutes=15), "shift closed", give_up_after=timedelta(hours=36))

    deferred = message(attempts=3)
    start = timeutils.now() + timedelta(seconds=1)
    run_at(monkeypatch, start)
    deferred.refresh_from_db()
    assert (deferred.status, deferred.attempts, deferred.last_error) == ("failed", 3, "shift closed")
    assert deferred.available_at == start + timedelta(minutes=15)
    assert deferred.deferred_since == start
    # the next deferral keeps the first refusal's instant (the give-up window counts from there)
    run_at(monkeypatch, start + timedelta(minutes=16))
    deferred.refresh_from_db()
    assert (deferred.attempts, deferred.deferred_since) == (3, start)
    assert deferred.available_at == start + timedelta(minutes=31)


def test_retry_later_gives_up_after_its_window(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    dead: list[str] = []

    @outbox.register("test.defer")
    def handler(_message: OutboxMessage) -> None:
        raise outbox.RetryLater(timedelta(minutes=15), "shift closed", give_up_after=timedelta(hours=36))

    @outbox.on_dead("test.defer")
    def hook(message_: OutboxMessage) -> None:
        dead.append(message_.last_error)

    start = timeutils.now() + timedelta(seconds=1)
    deferred = message(attempts=2, deferred_since=start - timedelta(hours=36), status="failed")
    with caplog.at_level(logging.ERROR):
        run_at(monkeypatch, start)
    deferred.refresh_from_db()
    assert (deferred.status, deferred.attempts) == ("dead", 3)
    assert deferred.last_error.startswith("shift closed (deferred since ")
    assert "gave up after 36 h)" in deferred.last_error
    entry = AuditEntry.objects.get(action="outbox.dead", entity_id=str(deferred.pk))
    assert entry.after is not None and entry.after["last_error"] == deferred.last_error
    assert dead == [deferred.last_error]
    assert "dead after its deferral window" in caplog.text


def test_retry_later_without_a_window_defers_for_ever(monkeypatch: pytest.MonkeyPatch) -> None:
    @outbox.register("test.defer")
    def handler(_message: OutboxMessage) -> None:
        raise outbox.RetryLater(timedelta(minutes=10), "payment outcome pending")

    start = timeutils.now() + timedelta(seconds=1)
    deferred = message(deferred_since=start - timedelta(days=30))
    run_at(monkeypatch, start)
    deferred.refresh_from_db()
    assert (deferred.status, deferred.attempts) == ("failed", 0)


def test_retry_later_needs_a_positive_delay() -> None:
    with pytest.raises(ValueError, match="positive"):
        outbox.RetryLater(timedelta(), "now")


@pytest.mark.parametrize("permanent", [False, True])
def test_a_nul_or_lone_surrogate_in_the_error_text_never_wedges_the_message(permanent: bool) -> None:
    """Review B13 (probe test_probe_outbox): the failure is recorded with a storable text instead of the
    UPDATE failing and the message staying in_flight to re-run at every lease expiry."""
    kind = outbox.PermanentFailure if permanent else RuntimeError

    @outbox.register("test.defer")
    def handler(_message: OutboxMessage) -> None:
        raise kind("Billz refused: x\x00y \ud800 z")

    failing = message()
    dispatch_outbox(batch=1)
    failing.refresh_from_db()
    assert failing.status == ("dead" if permanent else "failed")
    assert failing.last_error == "Billz refused: xy z"


def test_an_empty_error_text_names_the_exception() -> None:
    @outbox.register("test.defer")
    def handler(_message: OutboxMessage) -> None:
        raise RuntimeError("\x00")

    failing = message()
    dispatch_outbox(batch=1)
    failing.refresh_from_db()
    assert failing.last_error == "RuntimeError"


def test_text_helpers() -> None:
    assert clean_text("a\x00b\tc\r\nd\x7f\ud83d", 4) == "ab c"
    assert scrub_free_text("client with phone_number +998901234567 already exists") == (
        f"client with phone_number {MASKED_NUMBER} already exists"
    )
    assert scrub_free_text("call +998 (90) 123-45-67 or 90 123 45 67") == (
        f"call {MASKED_NUMBER} or {MASKED_NUMBER}"
    )
    assert scrub_free_text("order 10248, error 20036") == "order 10248, error 20036"  # short numbers stay
