"""The batched campaign send (design 03 §3.4–§3.6): cursor, idempotent redelivery, failures, beat start."""

from datetime import datetime, timedelta
from typing import Any

import pytest
from django.test import override_settings

from glowderma.accounts.tests.factories import CustomerFactory, DeviceFactory
from glowderma.core import outbox
from glowderma.core.models import AuditEntry, OutboxMessage
from glowderma.core.tasks import dispatch_outbox
from glowderma.core.timeutils import TASHKENT
from glowderma.integrations.fcm.fake import FakeFcm
from glowderma.integrations.fcm.ports import FcmUnavailable
from glowderma.notifications import services, tasks, topics
from glowderma.notifications.models import Campaign, CampaignRecipient, InboxNotification
from glowderma.notifications.tests.conftest import ANCHOR
from glowderma.notifications.tests.factories import (
    CampaignFactory,
    CustomerSegmentFactory,
    SegmentMemberFactory,
)

pytestmark = pytest.mark.django_db


def reachable(count: int, **device: Any) -> list[Any]:
    customers = [CustomerFactory.create() for _ in range(count)]
    for customer in customers:
        DeviceFactory.create(customer=customer, **device)
    return customers


def queued_campaign(**kwargs: Any) -> Campaign:
    campaign = CampaignFactory.create(**kwargs)
    with override_settings(CAMPAIGN_BATCH_SIZE=2):
        services.materialise(campaign)
    campaign.save()
    return campaign


@override_settings(CAMPAIGN_BATCH_SIZE=2)
def test_batches_follow_the_cursor_and_chain_through_the_outbox(clock: Any, fcm: FakeFcm) -> None:
    customers = reachable(5)
    campaign = queued_campaign()
    assert campaign.recipient_count == 5
    services.send_batch(campaign.pk, 0)
    campaign.refresh_from_db()
    assert (campaign.state, campaign.batches_dispatched, campaign.started_at) == ("sending", 1, ANCHOR)
    assert CampaignRecipient.objects.filter(campaign=campaign, state="sent").count() == 2
    assert campaign.send_cursor == CampaignRecipient.objects.filter(campaign=campaign).order_by("id")[1].pk
    next_message = OutboxMessage.objects.get(dedupe_key=topics.campaign_send_batch_key(campaign.pk, 1))
    assert next_message.payload == {"campaign_id": campaign.pk, "batch_no": 1}
    # the dispatcher runs the remaining batches (1 and 2) in one pass
    dispatch_outbox(batch=10)
    campaign.refresh_from_db()
    assert (campaign.state, campaign.sent, campaign.failed, campaign.batches_dispatched) == ("sent", 5, 0, 3)
    assert campaign.sent_at == ANCHOR
    assert InboxNotification.objects.filter(source_type="campaign", source_id=campaign.pk).count() == 5
    assert {m.token for m in fcm.sent} == {d.fcm_token for c in customers for d in c.devices.all()}
    assert set(OutboxMessage.objects.values_list("status", flat=True)) == {"done"}


@override_settings(CAMPAIGN_BATCH_SIZE=2)
def test_redelivered_batch_is_a_no_op_and_out_of_order_batches_wait(clock: Any, fcm: FakeFcm) -> None:
    reachable(3)
    campaign = queued_campaign()
    services.send_batch(campaign.pk, 0)
    sent_before = len(fcm.sent)
    services.send_batch(campaign.pk, 0)  # redelivery: nothing resent, nothing duplicated
    assert len(fcm.sent) == sent_before
    assert CampaignRecipient.objects.filter(campaign=campaign).count() == 3
    assert InboxNotification.objects.filter(source_id=campaign.pk).count() == 2
    campaign.refresh_from_db()
    assert campaign.batches_dispatched == 1
    with pytest.raises(RuntimeError, match="not ready"):
        services.send_batch(campaign.pk, 2)
    services.send_batch(campaign.pk, 1)
    campaign.refresh_from_db()
    assert campaign.state == "sent" and campaign.sent == 3
    services.send_batch(campaign.pk, 1)  # after completion: still a no-op
    assert len(fcm.sent) == 3 and InboxNotification.objects.filter(source_id=campaign.pk).count() == 3


def test_pending_recipient_with_a_claimed_or_settled_inbox_row_is_reconciled(
    clock: Any, fcm: FakeFcm
) -> None:
    """A crash between the claim and the settle leaves ``sending``: never resent; a settled row is adopted."""
    customers = reachable(3)
    campaign = queued_campaign()
    crashed = InboxNotification.objects.create(
        customer=customers[0],
        kind="general",
        title=campaign.title,
        body=campaign.body,
        link={"kind": "none"},
        source_type="campaign",
        source_id=campaign.pk,
        language="uz",
        dedupe_key=topics.campaign_inbox_key(campaign.pk, customers[0].pk),
        push_state="sending",
    )
    InboxNotification.objects.create(
        customer=customers[1],
        kind="general",
        title=campaign.title,
        body=campaign.body,
        link={"kind": "none"},
        source_type="campaign",
        source_id=campaign.pk,
        language="uz",
        dedupe_key=topics.campaign_inbox_key(campaign.pk, customers[1].pk),
        push_state="sent",
        pushed_at=ANCHOR - timedelta(minutes=5),
    )
    with override_settings(CAMPAIGN_BATCH_SIZE=10):
        services.send_batch(campaign.pk, 0)
    rows = {r.customer_id: r for r in CampaignRecipient.objects.filter(campaign=campaign)}
    assert (rows[customers[0].pk].state, rows[customers[0].pk].error) == ("failed", "interrupted")
    assert rows[customers[1].pk].state == "sent" and rows[customers[1].pk].sent_at == ANCHOR - timedelta(
        minutes=5
    )
    assert rows[customers[2].pk].state == "sent"
    assert len(fcm.sent) == 1
    crashed.refresh_from_db()
    assert crashed.push_state == "sending"
    campaign.refresh_from_db()
    assert (campaign.sent, campaign.failed) == (2, 1)


def test_devices_gone_between_materialise_and_send_are_skipped(clock: Any, fcm: FakeFcm) -> None:
    customers = reachable(2)
    campaign = queued_campaign()
    device = customers[0].devices.get()
    device.push_enabled = False
    device.fcm_token = ""
    device.save()
    with override_settings(CAMPAIGN_BATCH_SIZE=10):
        services.send_batch(campaign.pk, 0)
    skipped = CampaignRecipient.objects.get(campaign=campaign, customer=customers[0])
    assert (skipped.state, skipped.error) == ("skipped", "no_device")
    assert skipped.inbox is not None and skipped.inbox.push_state == "no_device"  # the feed stays complete
    campaign.refresh_from_db()
    assert (campaign.sent, campaign.failed) == (1, 1)


def test_provider_outage_releases_the_claims_and_retries(clock: Any, fcm: FakeFcm) -> None:
    customers = reachable(1)
    down = CustomerFactory.create()
    DeviceFactory.create(customer=down, fcm_token="tok-down")  # noqa: S106
    campaign = queued_campaign()
    with override_settings(CAMPAIGN_BATCH_SIZE=10), pytest.raises(FcmUnavailable):
        services.send_batch(campaign.pk, 0)
    campaign.refresh_from_db()
    assert campaign.state == "sending" and campaign.batches_dispatched == 0
    assert set(
        InboxNotification.objects.filter(source_id=campaign.pk).values_list("push_state", flat=True)
    ) == {"queued"}
    assert set(CampaignRecipient.objects.filter(campaign=campaign).values_list("state", flat=True)) == {
        "pending"
    }
    down.devices.update(fcm_token="tok-ok-now")  # noqa: S106
    with override_settings(CAMPAIGN_BATCH_SIZE=10):
        services.send_batch(campaign.pk, 0)
    campaign.refresh_from_db()
    assert (campaign.state, campaign.sent, campaign.failed) == ("sent", 2, 0)
    assert {m.token for m in fcm.sent} == {customers[0].devices.get().fcm_token, "tok-ok-now"}


def test_start_due_campaigns_only_when_due_and_recomputes_recipients(clock: Any, fcm: FakeFcm) -> None:
    segment = CustomerSegmentFactory.create(min_online_orders=None)
    campaign = CampaignFactory.create(
        state=Campaign.State.SCHEDULED,
        scheduled_at=datetime(2026, 10, 26, 10, tzinfo=TASHKENT),
        audience_type="segment",
        audience_segment=segment,
        audience_segment_name=segment.name,
        recipient_count=612,
    )
    assert services.start_due_campaigns() == []
    clock(datetime(2026, 10, 26, 9, 59, tzinfo=TASHKENT))
    assert tasks.start_due_campaigns() == []
    campaign.refresh_from_db()
    assert campaign.state == "scheduled" and OutboxMessage.objects.count() == 0
    members = reachable(2)
    for customer in members:
        SegmentMemberFactory.create(segment=segment, customer=customer)
    clock(datetime(2026, 10, 26, 10, 0, tzinfo=TASHKENT))
    assert services.start_due_campaigns() == [campaign.pk]
    campaign.refresh_from_db()
    assert campaign.state == "queued" and campaign.recipient_count == 2  # recomputed from 612
    message = OutboxMessage.objects.get(topic="campaign.send_batch")
    assert message.payload == {"campaign_id": campaign.pk, "batch_no": 0}
    assert services.start_due_campaigns() == []  # no longer scheduled
    dispatch_outbox(batch=5)
    campaign.refresh_from_db()
    assert (campaign.state, campaign.sent) == ("sent", 2)


def test_due_campaign_without_recipients_is_sent_immediately(clock: Any) -> None:
    campaign = CampaignFactory.create(
        state=Campaign.State.SCHEDULED, scheduled_at=ANCHOR - timedelta(minutes=1), recipient_count=5
    )
    assert services.start_due_campaigns() == [campaign.pk]
    campaign.refresh_from_db()
    assert (campaign.state, campaign.recipient_count, campaign.sent, campaign.failed) == ("sent", 0, 0, 0)
    assert campaign.started_at == campaign.sent_at == ANCHOR and OutboxMessage.objects.count() == 0


def test_handler_registration_and_dead_letter_audit(clock: Any) -> None:
    for topic in topics.HANDLED_TOPICS:
        expected = (
            tasks.on_account_security_dead if topic == topics.PUSH_ACCOUNT_SECURITY else tasks.on_push_dead
        )
        assert topic in outbox._handlers and outbox._dead_handlers[topic] is expected
    message = OutboxMessage.objects.create(
        topic="push.order_status",
        payload={"order_id": 10248, "status": "processing", "event_id": 3, "customer_id": 99},
        dedupe_key="push.order_status:10248:processing",
        attempts=9,
        status="failed",
        last_error="boom",
    )
    tasks.on_push_dead(message)
    entry = AuditEntry.objects.get(action="notification.push_dead")
    assert (entry.entity_type, entry.entity_id, entry.actor_type) == ("outbox", str(message.pk), "system")
    assert entry.after == {
        "topic": "push.order_status",
        "dedupe_key": "push.order_status:10248:processing",
        "attempts": 9,
        "last_error": "boom",
        "customer_id": 99,
        "campaign_id": None,
    }


def test_send_batch_handler_reads_the_payload(clock: Any, fcm: FakeFcm) -> None:
    reachable(1)
    campaign = queued_campaign()
    message = OutboxMessage.objects.create(
        topic="campaign.send_batch",
        payload={"campaign_id": campaign.pk, "batch_no": 0},
        dedupe_key=topics.campaign_send_batch_key(campaign.pk, 0),
    )
    with override_settings(CAMPAIGN_BATCH_SIZE=10):
        tasks.handle_campaign_send_batch(message)
    campaign.refresh_from_db()
    assert campaign.state == "sent" and campaign.sent == 1


def test_send_batch_on_a_scheduled_campaign_is_refused(clock: Any) -> None:
    campaign = CampaignFactory.create(
        state=Campaign.State.SCHEDULED, scheduled_at=ANCHOR + timedelta(hours=1)
    )
    with pytest.raises(RuntimeError):
        services.send_batch(campaign.pk, 0)


class ReenteringFcm(FakeFcm):
    """Simulates an expired outbox lease: while worker A is inside the provider call, worker B re-runs the
    same batch (once) before A's call returns."""

    def __init__(self, campaign_id: int, batch_no: int) -> None:
        super().__init__()
        self.campaign_id, self.batch_no, self.reentered = campaign_id, batch_no, False

    def send(self, messages: Any) -> Any:
        if not self.reentered:
            self.reentered = True
            services.send_batch(self.campaign_id, self.batch_no)  # worker B
        return super().send(messages)


@override_settings(CAMPAIGN_BATCH_SIZE=2)
def test_an_overlapping_redelivery_of_a_middle_batch_does_not_strand_the_campaign(
    clock: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from glowderma.integrations.fcm import registry

    reachable(3)
    campaign = queued_campaign()
    fake = ReenteringFcm(campaign.pk, 0)
    monkeypatch.setattr(registry, "_fake", fake)
    services.send_batch(campaign.pk, 0)  # worker A
    campaign.refresh_from_db()
    assert fake.reentered and campaign.batches_dispatched == 1 and campaign.state == "sending"
    first_two = list(CampaignRecipient.objects.filter(campaign=campaign).order_by("id")[:2])
    assert [r.state for r in first_two] == ["sent", "sent"]  # A's settle wins over B's "interrupted"
    assert all(r.error == "" for r in first_two)
    next_batch = OutboxMessage.objects.filter(topic="campaign.send_batch")
    assert [m.payload["batch_no"] for m in next_batch] == [1]  # enqueued exactly once
    dispatch_outbox(batch=10)  # batch 1 still runs
    campaign.refresh_from_db()
    assert (campaign.state, campaign.sent, campaign.failed, campaign.batches_dispatched) == ("sent", 3, 0, 2)
    assert set(CampaignRecipient.objects.filter(campaign=campaign).values_list("state", flat=True)) == {
        "sent"
    }
    assert set(
        InboxNotification.objects.filter(source_id=campaign.pk).values_list("push_state", flat=True)
    ) == {"sent"}
    assert len(fake.sent) == 3  # nobody pushed twice


def test_an_overlapping_redelivery_of_the_last_batch_refreshes_the_final_tally(
    clock: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from glowderma.integrations.fcm import registry

    reachable(3)
    campaign = queued_campaign()
    fake = ReenteringFcm(campaign.pk, 0)
    monkeypatch.setattr(registry, "_fake", fake)
    with override_settings(CAMPAIGN_BATCH_SIZE=10):
        services.send_batch(campaign.pk, 0)
    campaign.refresh_from_db()
    # B finished the campaign while A's claims were in flight (counted as interrupted); A's settle corrects it
    assert (campaign.state, campaign.sent, campaign.failed, campaign.batches_dispatched) == ("sent", 3, 0, 1)
    assert set(CampaignRecipient.objects.filter(campaign=campaign).values_list("state", flat=True)) == {
        "sent"
    }
    assert not OutboxMessage.objects.filter(topic="campaign.send_batch").exists()
    assert len(fake.sent) == 3


def test_push_topics_declare_their_deferral_window_for_the_consistency_report() -> None:
    """M3 integration: a push FCM keeps deferring is buried after ``NOT_SENT_GIVE_UP`` (6 h), so the nightly
    report (K6 ``deferred_past_window``) measures these topics against 6 h, not the 24 h default."""
    from glowderma.core import consistency

    windows = outbox.deferral_windows()
    for topic in (
        topics.PUSH_ORDER_STATUS,
        topics.PUSH_APPOINTMENT,
        topics.PUSH_ACCOUNT_SECURITY,
        topics.CAMPAIGN_SEND_BATCH,
    ):
        assert windows[topic] == services.NOT_SENT_GIVE_UP == timedelta(hours=6)
    stuck = OutboxMessage.objects.create(
        topic=topics.PUSH_ORDER_STATUS,
        payload={},
        dedupe_key="push.order_status:k6",
        status="failed",
        deferred_since=ANCHOR - timedelta(hours=7),
    )
    fresh = OutboxMessage.objects.create(
        topic=topics.PUSH_ORDER_STATUS,
        payload={},
        dedupe_key="push.order_status:k6-fresh",
        status="failed",
        deferred_since=ANCHOR - timedelta(hours=5),
    )
    found = consistency.deferred_past_window(ANCHOR, timedelta(minutes=30), limit=10)
    assert stuck.pk in found.ids and fresh.pk not in found.ids
