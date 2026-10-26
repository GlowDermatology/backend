"""``manage.py requeue_outbox`` (decision D3): replay dead messages of one topic, audited."""

from datetime import timedelta
from io import StringIO

import pytest
from django.core.management import CommandError, call_command

from glowderma.core import timeutils
from glowderma.core.models import AuditEntry, OutboxMessage

pytestmark = pytest.mark.django_db


def dead(key: str, *, topic: str = "billz.push_sale", died_at: object = None) -> OutboxMessage:
    message = OutboxMessage.objects.create(
        topic=topic,
        payload={"order_id": 1},
        dedupe_key=key,
        status="dead",
        attempts=1,
        last_error="POST /v2/order: the integration key may not call this route",
        deferred_since=timeutils.now(),
    )
    if died_at is not None:
        OutboxMessage.objects.filter(pk=message.pk).update(updated_at=died_at)
    return message


def requeue(*args: str) -> str:
    out = StringIO()
    call_command("requeue_outbox", *args, stdout=out)
    return out.getvalue()


def test_requeues_the_dead_messages_of_one_topic_and_audits_each() -> None:
    first = dead("billz.push_sale:a")
    second = dead("billz.push_sale:b")
    other_topic = dead("bts:1", topic="bts.create_shipment")
    alive = OutboxMessage.objects.create(topic="billz.push_sale", payload={}, dedupe_key="billz.push_sale:c")
    output = requeue("--topic", "billz.push_sale", "--dead")
    assert "billz.push_sale: 2 dead message(s) matched, 2 requeued." in output
    assert f"requeue #{first.pk} billz.push_sale:a (attempts 1)" in output
    for message in (first, second):
        message.refresh_from_db()
        assert (message.status, message.attempts, message.deferred_since) == ("pending", 0, None)
        assert message.available_at <= timeutils.now()
        entry = AuditEntry.objects.get(action="outbox.requeued", entity_id=str(message.pk))
        assert entry.actor_type == "system" and entry.reason == "manage.py requeue_outbox"
        assert entry.before is not None and entry.before["status"] == "dead"
    other_topic.refresh_from_db()
    alive.refresh_from_db()
    assert (other_topic.status, alive.status) == ("dead", "pending")


def test_dry_run_changes_nothing_and_since_filters() -> None:
    old = dead("billz.push_sale:old", died_at=timeutils.now() - timedelta(days=3))
    recent = dead("billz.push_sale:new")
    since = (timeutils.now() - timedelta(days=1)).isoformat()
    output = requeue("--topic", "billz.push_sale", "--dead", "--since", since, "--dry-run")
    assert f"would requeue #{recent.pk}" in output and f"#{old.pk} " not in output
    assert "1 dead message(s) matched, 0 requeued (dry run: nothing changed)." in output
    assert OutboxMessage.objects.filter(status="dead").count() == 2
    assert not AuditEntry.objects.filter(action="outbox.requeued").exists()


@pytest.mark.parametrize(
    ("args", "message"),
    [
        (["--topic", "billz.push_sale"], "pass --dead"),
        (["--topic", " ", "--dead"], "--topic must name"),
        (["--topic", "billz.push_sale", "--dead", "--since", "yesterday"], "ISO 8601"),
        (["--topic", "billz.push_sale", "--dead", "--since", "2026-10-26T09:00:00"], "UTC offset"),
    ],
)
def test_refuses_unclear_requests(args: list[str], message: str) -> None:
    with pytest.raises(CommandError, match=message):
        requeue(*args)
