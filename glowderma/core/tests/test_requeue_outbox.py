"""``manage.py requeue_outbox`` (decision D3): replay dead messages of one topic, audited."""

from datetime import timedelta
from io import StringIO

import pytest
from django.core.management import CommandError, call_command

from glowderma.core import outbox, timeutils
from glowderma.core.models import AuditEntry, OutboxMessage

pytestmark = pytest.mark.django_db


TOPIC = "demo.sync"  # a topic without requeue hooks: the command's own behaviour


def dead(key: str, *, topic: str = TOPIC, died_at: object = None) -> OutboxMessage:
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
    first = dead("demo.sync:a")
    second = dead("demo.sync:b")
    other_topic = dead("bts:1", topic="bts.create_shipment")
    alive = OutboxMessage.objects.create(topic="demo.sync", payload={}, dedupe_key="demo.sync:c")
    output = requeue("--topic", "demo.sync", "--dead")
    assert "demo.sync: 2 dead message(s) matched, 2 requeued, 0 skipped." in output
    assert f"requeue #{first.pk} demo.sync:a (attempts 1)" in output
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
    old = dead("demo.sync:old", died_at=timeutils.now() - timedelta(days=3))
    recent = dead("demo.sync:new")
    since = (timeutils.now() - timedelta(days=1)).isoformat()
    output = requeue("--topic", "demo.sync", "--dead", "--since", since, "--dry-run")
    assert f"would requeue #{recent.pk}" in output and f"#{old.pk} " not in output
    assert "1 dead message(s) matched, 0 requeued, 0 skipped (dry run: nothing changed)." in output
    assert OutboxMessage.objects.filter(status="dead").count() == 2
    assert not AuditEntry.objects.filter(action="outbox.requeued").exists()


@pytest.mark.parametrize(
    ("args", "message"),
    [
        (["--topic", "demo.sync"], "pass --dead"),
        (["--topic", " ", "--dead"], "--topic must name"),
        (["--topic", "demo.sync", "--dead", "--since", "yesterday"], "ISO 8601"),
        (["--topic", "demo.sync", "--dead", "--since", "2026-10-26T09:00:00"], "UTC offset"),
        (["--topic", "demo.sync", "--dead", "--forget-payment-attempt", "--order", "1"], "not supported"),
        (["--topic", "billz.push_sale", "--dead", "--forget-payment-attempt"], "needs --order"),
        (
            ["--topic", "billz.push_sale", "--dead", "--forget-payment-attempt", "--order", "1"],
            "needs --actor",
        ),
        (
            [
                "--topic",
                "billz.push_sale",
                "--dead",
                "--forget-payment-attempt",
                "--order",
                "1",
                "--actor",
                "x@y.z",
            ],
            "no active staff member",
        ),
    ],
)
def test_refuses_unclear_requests(args: list[str], message: str) -> None:
    with pytest.raises(CommandError, match=message):
        requeue(*args)


def test_topic_hooks_describe_skip_and_warn() -> None:
    """NEW-4: the module owning a topic says what each message is about, skips one done another way and
    prints a warning first; ``--order`` keeps one order's messages."""
    outbox.on_requeue(
        "demo.hooked",
        outbox.RequeueHooks(
            describe=lambda message: f"order {message.payload['order_id']}",
            skip_reason=lambda message: "done by hand" if message.payload["order_id"] == 2 else "",
            warning="check the provider first",
        ),
    )
    try:
        kept = dead("demo.hooked:1", topic="demo.hooked")
        done = dead("demo.hooked:2", topic="demo.hooked")
        OutboxMessage.objects.filter(pk=done.pk).update(payload={"order_id": 2})
        dead("demo.hooked:3", topic="demo.hooked")
        OutboxMessage.objects.filter(dedupe_key="demo.hooked:3").update(payload={"order_id": 3})
        output = requeue("--topic", "demo.hooked", "--dead", "--order", "1")
        assert output.startswith("WARNING: check the provider first\n")
        assert f"requeue #{kept.pk} demo.hooked:1 [order 1] (attempts 1)" in output
        assert "1 dead message(s) matched, 1 requeued, 0 skipped." in output
        output = requeue("--topic", "demo.hooked", "--dead")
        assert f"skip #{done.pk} demo.hooked:2 [order 2]: done by hand" in output
        assert "2 dead message(s) matched, 1 requeued, 1 skipped." in output
        done.refresh_from_db()
        assert done.status == "dead"
    finally:
        outbox._requeue_hooks.pop("demo.hooked", None)
