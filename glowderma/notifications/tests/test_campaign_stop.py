"""The emergency stop (admin contract §2.8 ``POST notifications/{id}/stop/``) and the dead-batch closure:
``sending`` → ``stopping`` → ``partially_sent`` / ``cancelled``; pending recipients ``stopped``;
``attempted == sent + failed``; a stop during the FCM call; the sweeper; a dead ``campaign.send_batch``."""

from datetime import timedelta
from typing import Any

import pytest
from django.test import override_settings
from rest_framework.test import APIClient

from glowderma.accounts.models import StaffUser
from glowderma.accounts.tests.factories import CustomerFactory, DeviceFactory
from glowderma.core import outbox
from glowderma.core.models import AuditEntry, OutboxMessage
from glowderma.core.tasks import dispatch_outbox
from glowderma.integrations.fcm import registry
from glowderma.integrations.fcm.fake import FakeFcm
from glowderma.notifications import services, tasks, topics
from glowderma.notifications.models import Campaign, CampaignRecipient, InboxNotification
from glowderma.notifications.tests.conftest import ANCHOR
from glowderma.notifications.tests.factories import (
    CampaignFactory,
    ScheduledCampaignFactory,
    SendingCampaignFactory,
    SentCampaignFactory,
)

pytestmark = pytest.mark.django_db

URL = "/api/v1/admin/notifications/"


def reachable(count: int, **device: Any) -> list[Any]:
    customers = [CustomerFactory.create() for _ in range(count)]
    for customer in customers:
        DeviceFactory.create(customer=customer, **device)
    return customers


def sending_campaign(**kwargs: Any) -> Campaign:
    """A campaign the beat started: recipients materialised, tallies at 0, batch 0 not yet run."""
    campaign = SendingCampaignFactory.create(**kwargs)
    services.materialise(campaign)
    campaign.save()
    return campaign


def states(campaign: Campaign) -> dict[str, int]:
    tally: dict[str, int] = {}
    for value in CampaignRecipient.objects.filter(campaign=campaign).values_list("state", flat=True):
        tally[value] = tally.get(value, 0) + 1
    return tally


def stop(client: APIClient, campaign_id: int) -> Any:
    return client.post(f"{URL}{campaign_id}/stop/", {}, format="json")


@override_settings(CAMPAIGN_BATCH_SIZE=2)
def test_stop_between_two_batches_leaves_the_rest_stopped(
    superuser_client: APIClient, superuser: StaffUser, clock: Any, fcm: FakeFcm
) -> None:
    reachable(5)
    campaign = sending_campaign()
    services.send_batch(campaign.pk, 0)
    campaign.refresh_from_db()
    assert (campaign.state, campaign.sent, campaign.failed, campaign.batches_dispatched) == (
        "sending",
        2,
        0,
        1,
    )
    response = stop(superuser_client, campaign.pk)
    assert response.status_code == 200, response.data
    assert response.data["state"] == "stopping" and response.data["stop_requested_at"] == ANCHOR.isoformat()
    assert response.data["stop_requested_by"] == {"id": superuser.pk, "display_name": "Egasi"}
    assert (response.data["sent"], response.data["failed"], response.data["attempted"]) == (2, 0, 2)
    entry = AuditEntry.objects.get(action="notification.stop_requested")
    assert entry.actor_id == superuser.pk and entry.before == {"state": "sending", "sent": 2, "failed": 0}
    # the next batch (already enqueued) finalises instead of claiming
    dispatch_outbox(batch=10)
    campaign.refresh_from_db()
    assert campaign.state == "partially_sent" and campaign.stopped_at == ANCHOR
    assert (campaign.sent, campaign.failed) == (2, 0) and campaign.sent_at is None
    assert states(campaign) == {"sent": 2, "stopped": 3}
    assert set(CampaignRecipient.objects.filter(state="stopped").values_list("error", flat=True)) == {
        "stopped"
    }
    assert len(fcm.sent) == 2 and InboxNotification.objects.filter(source_id=campaign.pk).count() == 2
    assert not OutboxMessage.objects.filter(topic="campaign.send_batch").exclude(status="done").exists()
    stopped = AuditEntry.objects.get(action="notification.stopped")
    assert stopped.actor_type == "system" and stopped.after is not None
    assert (stopped.after["state"], stopped.after["sent"], stopped.after["failed"]) == (
        "partially_sent",
        2,
        0,
    )
    record = superuser_client.get(f"{URL}{campaign.pk}/").data
    assert record["attempted"] == record["sent"] + record["failed"] == 2
    assert record["stopped_at"] == ANCHOR.isoformat() and record["cancelled_at"] is None
    # idempotent: a stop on a campaign that ended by a stop answers unchanged, no second audit row
    again = stop(superuser_client, campaign.pk)
    assert again.status_code == 200 and again.data["state"] == "partially_sent"
    assert AuditEntry.objects.filter(action="notification.stop_requested").count() == 1


def test_stop_before_anything_went_out_cancels_the_campaign(
    superuser_client: APIClient, superuser: StaffUser, clock: Any, fcm: FakeFcm
) -> None:
    reachable(3)
    campaign = sending_campaign()
    assert stop(superuser_client, campaign.pk).data["state"] == "stopping"
    with override_settings(CAMPAIGN_BATCH_SIZE=10):
        services.send_batch(campaign.pk, 0)  # the first batch meets the stop before claiming
    campaign.refresh_from_db()
    assert campaign.state == "cancelled" and campaign.cancelled_at == ANCHOR
    assert campaign.cancelled_by == superuser and campaign.stopped_at == ANCHOR
    assert (campaign.sent, campaign.failed) == (0, 0) and states(campaign) == {"stopped": 3}
    assert fcm.sent == [] and not InboxNotification.objects.exists()
    listed = superuser_client.get(URL, {"state": "cancelled"}).data["results"]
    assert [row["id"] for row in listed] == [campaign.pk] and listed[0]["attempted"] == 0


class StoppingFcm(FakeFcm):
    """The superuser presses the stop button while the batch is inside the provider call; FCM also leaves
    one push of the batch untaken (``-busy``)."""

    def __init__(self, campaign_id: int, actor: Any) -> None:
        super().__init__()
        self.campaign_id, self.actor = campaign_id, actor

    def send(self, messages: Any) -> Any:
        services.request_stop(self.campaign_id, actor=self.actor)
        return super().send(messages)


@override_settings(CAMPAIGN_BATCH_SIZE=3)
def test_a_stop_during_the_fcm_call_settles_the_batch_and_finalises(
    superuser: StaffUser, clock: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    customers = reachable(2)
    busy = CustomerFactory.create()
    DeviceFactory.create(customer=busy, fcm_token="tok-busy")  # noqa: S106
    reachable(2)  # the second batch, never started
    campaign = sending_campaign()
    fake = StoppingFcm(campaign.pk, superuser)
    monkeypatch.setattr(registry, "_fake", fake)
    services.send_batch(campaign.pk, 0)  # no RetryLater: the untaken push is stopped, not deferred
    campaign.refresh_from_db()
    assert campaign.state == "partially_sent" and campaign.batches_dispatched == 1
    assert (campaign.sent, campaign.failed) == (2, 0) and campaign.stopped_at == ANCHOR
    rows = {r.customer_id: r for r in CampaignRecipient.objects.filter(campaign=campaign)}
    assert all(rows[c.pk].state == "sent" for c in customers)
    assert (rows[busy.pk].state, rows[busy.pk].error) == ("stopped", "stopped")
    # the untaken push never reached the device: its in-app row is withdrawn with the campaign
    assert rows[busy.pk].inbox is None and not InboxNotification.objects.filter(customer=busy).exists()
    assert states(campaign) == {"sent": 2, "stopped": 3}
    assert not OutboxMessage.objects.filter(topic="campaign.send_batch").exists()  # no next batch
    assert len(fake.sent) == 2


@override_settings(CAMPAIGN_BATCH_SIZE=2)
def test_a_stop_withdraws_the_in_app_rows_of_recipients_never_pushed(
    superuser_client: APIClient, clock: Any, fcm: FakeFcm
) -> None:
    """A recipient FCM gave back (``not_sent``: inbox ``queued``, recipient ``pending``) must not keep the
    stopped campaign in the in-app feed («qolgan qurilmalarga xabar bormaydi»); the pushed one keeps it."""
    [pushed] = reachable(1)
    [given_back] = reachable(1, fcm_token="tok-busy")  # noqa: S106
    reachable(1)  # the second batch, never claimed
    campaign = sending_campaign()
    with pytest.raises(outbox.RetryLater):
        services.send_batch(campaign.pk, 0)
    assert InboxNotification.objects.get(customer=given_back).push_state == "queued"
    assert stop(superuser_client, campaign.pk).status_code == 200
    services.send_batch(campaign.pk, 0)  # the redelivery finds ``stopping`` and finalises
    campaign.refresh_from_db()
    assert campaign.state == "partially_sent" and (campaign.sent, campaign.failed) == (1, 0)
    rows = {r.customer_id: r for r in CampaignRecipient.objects.filter(campaign=campaign)}
    assert rows[given_back.pk].state == "stopped" and rows[given_back.pk].inbox is None
    assert not InboxNotification.objects.filter(customer=given_back).exists()
    assert InboxNotification.objects.get(customer=pushed).push_state == "sent"
    assert InboxNotification.objects.filter(source_id=campaign.pk).count() == 1


def test_a_dead_batch_withdraws_the_in_app_rows_of_recipients_never_pushed(clock: Any, fcm: FakeFcm) -> None:
    [pushed] = reachable(1)
    [given_back] = reachable(1, fcm_token="tok-busy")  # noqa: S106
    campaign = sending_campaign()
    with pytest.raises(outbox.RetryLater):
        services.send_batch(campaign.pk, 0)
    message = OutboxMessage.objects.create(  # the deferred batch 0 the dispatcher gave up on
        topic=topics.CAMPAIGN_SEND_BATCH,
        payload={"campaign_id": campaign.pk, "batch_no": 0},
        dedupe_key=topics.campaign_send_batch_key(campaign.pk, 0),
        status="dead",
        last_error="fake provider busy",
    )
    tasks.on_push_dead(message)
    campaign.refresh_from_db()
    assert (campaign.state, campaign.sent, campaign.failed) == ("partially_sent", 1, 0)
    assert states(campaign) == {"sent": 1, "stopped": 1}
    assert not InboxNotification.objects.filter(customer=given_back).exists()
    assert InboxNotification.objects.get(customer=pushed).push_state == "sent"


def test_request_stop_is_refused_outside_sending(superuser_client: APIClient, clock: Any) -> None:
    for campaign in (
        CampaignFactory.create(),
        ScheduledCampaignFactory.create(),
        SentCampaignFactory.create(),
    ):
        response = stop(superuser_client, campaign.pk)
        assert response.status_code == 409, response.data
        assert (response.data["code"], response.data["message"]) == (
            "invalid_transition",
            services.MESSAGE_NOT_SENDING,
        )
    assert stop(superuser_client, 999_999).status_code == 404
    stopping = SendingCampaignFactory.create(state="stopping", stop_requested_at=ANCHOR)
    assert stop(superuser_client, stopping.pk).status_code == 200  # idempotent
    assert not AuditEntry.objects.filter(action="notification.stop_requested").exists()


def test_the_sweeper_finalises_a_stopping_campaign_without_an_open_batch(
    superuser: StaffUser, clock: Any
) -> None:
    reachable(2)
    orphan = sending_campaign()
    services.request_stop(orphan.pk, actor=superuser)
    busy = sending_campaign()
    services.request_stop(busy.pk, actor=superuser)
    OutboxMessage.objects.create(  # a batch still in flight: the sweeper leaves this campaign to it
        topic=topics.CAMPAIGN_SEND_BATCH,
        payload={"campaign_id": busy.pk, "batch_no": 0},
        dedupe_key=topics.campaign_send_batch_key(busy.pk, 0),
        status="in_flight",
    )
    assert tasks.finalise_stopping_campaigns() == [orphan.pk]
    orphan.refresh_from_db()
    busy.refresh_from_db()
    assert (
        orphan.state == "cancelled" and orphan.cancelled_by == superuser and states(orphan) == {"stopped": 2}
    )
    assert busy.state == "stopping"
    assert services.finalise_stopping_campaigns() == []  # nothing left to do


def test_a_dead_batch_closes_the_campaign(clock: Any, fcm: FakeFcm) -> None:
    reachable(2)
    partially = sending_campaign()
    with override_settings(CAMPAIGN_BATCH_SIZE=1):
        services.send_batch(partially.pk, 0)
    message = OutboxMessage.objects.get(dedupe_key=topics.campaign_send_batch_key(partially.pk, 1))
    message.status, message.attempts, message.last_error = "dead", 10, "fake provider down"
    message.save()
    tasks.on_push_dead(message)
    partially.refresh_from_db()
    assert (partially.state, partially.sent, partially.failed) == ("partially_sent", 1, 0)
    assert partially.failed_at == ANCHOR and partially.last_error == "fake provider down"
    assert partially.stopped_at is None and states(partially) == {"sent": 1, "stopped": 1}
    failed_entry = AuditEntry.objects.get(action="notification.failed", entity_id=str(partially.pk))
    assert failed_entry.actor_type == "system" and failed_entry.after is not None
    assert failed_entry.after["outbox_message_id"] == message.pk
    assert AuditEntry.objects.filter(action="notification.push_dead").count() == 1
    # nothing went out: failed
    nothing = sending_campaign()
    dead = OutboxMessage.objects.create(
        topic=topics.CAMPAIGN_SEND_BATCH,
        payload={"campaign_id": nothing.pk, "batch_no": 0},
        dedupe_key=topics.campaign_send_batch_key(nothing.pk, 0),
        status="dead",
        last_error="x" * 3000,
    )
    tasks.on_push_dead(dead)
    nothing.refresh_from_db()
    assert (nothing.state, nothing.sent, nothing.failed) == ("failed", 0, 0)
    assert len(nothing.last_error) == 2000 and states(nothing) == {"stopped": 2}
    # idempotent, and a finished campaign is left alone
    tasks.on_push_dead(dead)
    assert AuditEntry.objects.filter(action="notification.failed").count() == 2
    sent = SentCampaignFactory.create()
    tasks.on_push_dead(
        OutboxMessage.objects.create(
            topic=topics.CAMPAIGN_SEND_BATCH,
            payload={"campaign_id": sent.pk, "batch_no": 3},
            dedupe_key=topics.campaign_send_batch_key(sent.pk, 3),
            status="dead",
        )
    )
    sent.refresh_from_db()
    assert sent.state == "sent" and AuditEntry.objects.filter(action="notification.failed").count() == 2
    tasks.on_push_dead(OutboxMessage(topic=topics.CAMPAIGN_SEND_BATCH, payload={}, dedupe_key="x"))


def test_the_dispatcher_buries_a_deferred_batch_and_the_campaign_follows(clock: Any, fcm: FakeFcm) -> None:
    """End to end through the outbox: a batch deferred past ``NOT_SENT_GIVE_UP`` dies and the hook closes the
    campaign (one push went out, one FCM kept refusing: ``partially_sent``)."""
    reachable(1)
    busy = CustomerFactory.create()
    DeviceFactory.create(customer=busy, fcm_token="tok-busy")  # noqa: S106
    campaign = sending_campaign()
    outbox.enqueue(
        topics.CAMPAIGN_SEND_BATCH,
        {"campaign_id": campaign.pk, "batch_no": 0},
        dedupe_key=topics.campaign_send_batch_key(campaign.pk, 0),
    )
    dispatch_outbox(batch=5)
    message = OutboxMessage.objects.get(topic=topics.CAMPAIGN_SEND_BATCH)
    assert message.status == "failed" and message.deferred_since == ANCHOR
    campaign.refresh_from_db()
    assert (campaign.state, campaign.sent) == ("sending", 1)  # the deferral keeps the campaign open
    # the retried batch holds the one refused push only: FCM "took nothing" is a plain failure, and the
    # tenth one buries the message
    OutboxMessage.objects.filter(pk=message.pk).update(attempts=outbox.MAX_ATTEMPTS - 1)
    clock(ANCHOR + services.NOT_SENT_RETRY + timedelta(minutes=1))
    dispatch_outbox(batch=5)
    message.refresh_from_db()
    assert message.status == "dead"
    campaign.refresh_from_db()
    assert campaign.state == "partially_sent" and campaign.failed_at is not None
    assert (campaign.sent, campaign.failed) == (1, 0) and states(campaign) == {"sent": 1, "stopped": 1}
