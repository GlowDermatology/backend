"""Contract §2.8 ``POST notifications/{id}/cancel/`` (M3): a scheduled campaign is cancelled before its send
starts — idempotent, audited, 409 once the send started; the beat never starts a cancelled campaign."""

from datetime import datetime, timedelta
from typing import Any

import pytest
from rest_framework.test import APIClient

from glowderma.accounts.models import StaffUser
from glowderma.accounts.tests.factories import CustomerFactory, DeviceFactory
from glowderma.core import labels
from glowderma.core.models import AuditEntry, OutboxMessage
from glowderma.core.timeutils import TASHKENT
from glowderma.notifications import services
from glowderma.notifications.models import Campaign, CampaignRecipient
from glowderma.notifications.tests.conftest import ANCHOR
from glowderma.notifications.tests.factories import (
    CampaignFactory,
    CustomerSegmentFactory,
    SentCampaignFactory,
)

pytestmark = pytest.mark.django_db

URL = "/api/v1/admin/notifications/"
AT = datetime(2026, 10, 27, 10, tzinfo=TASHKENT)


def scheduled(**overrides: Any) -> Campaign:
    return CampaignFactory.create(state=Campaign.State.SCHEDULED, scheduled_at=AT, **overrides)


def cancel(client: APIClient, campaign_id: int) -> Any:
    return client.post(f"{URL}{campaign_id}/cancel/", format="json")


def test_a_scheduled_campaign_is_cancelled_once_and_audited(
    staff_client: APIClient, staff: StaffUser, clock: Any
) -> None:
    campaign = scheduled(author=staff)
    response = cancel(staff_client, campaign.pk)
    assert response.status_code == 200, response.data
    assert response.data["id"] == campaign.pk and response.data["state"] == "cancelled"
    assert response.data["cancelled_at"] == "2026-10-24T12:05:00+05:00" == ANCHOR.isoformat()
    assert (response.data["sent_at"], response.data["sent"], response.data["failed"]) == (None, None, None)
    campaign.refresh_from_db()
    assert (campaign.state, campaign.cancelled_at) == ("cancelled", ANCHOR)
    entry = AuditEntry.objects.get(action="notification.cancelled", entity_id=str(campaign.pk))
    assert entry.actor_id == staff.pk
    assert entry.before == {"state": "scheduled", "scheduled_at": AT.isoformat()}
    assert entry.after == {"state": "cancelled", "cancelled_at": ANCHOR.isoformat()}
    # idempotent: the same answer, no second audit row
    again = cancel(staff_client, campaign.pk)
    assert again.status_code == 200 and again.data == response.data
    assert AuditEntry.objects.filter(action="notification.cancelled").count() == 1
    listed = staff_client.get(URL, {"state": "cancelled"}).json()["results"]
    assert [row["id"] for row in listed] == [campaign.pk]
    assert staff_client.get(URL, {"state": "scheduled"}).json()["results"] == []
    assert labels.NOTIFICATION_STATE["cancelled"] == "Bekor qilindi"


@pytest.mark.parametrize("state", [Campaign.State.QUEUED, Campaign.State.SENDING, Campaign.State.SENT])
def test_a_campaign_whose_send_started_cannot_be_cancelled(
    staff_client: APIClient, state: str, clock: Any
) -> None:
    if state == Campaign.State.SENT:
        campaign = SentCampaignFactory.create()
    elif state == Campaign.State.SENDING:
        campaign = CampaignFactory.create(state=state, started_at=ANCHOR - timedelta(minutes=1))
    else:
        campaign = CampaignFactory.create(state=state)
    response = cancel(staff_client, campaign.pk)
    assert response.status_code == 409
    assert (response.data["code"], response.data["message"]) == ("conflict", services.MESSAGE_CANCEL_STARTED)
    campaign.refresh_from_db()
    assert campaign.state == state and campaign.cancelled_at is None
    assert not AuditEntry.objects.filter(action="notification.cancelled").exists()


def test_unknown_campaign_is_404(staff_client: APIClient) -> None:
    assert cancel(staff_client, 999_999).status_code == 404


def test_the_beat_never_starts_a_cancelled_campaign(staff_client: APIClient, clock: Any) -> None:
    customer = CustomerFactory.create()
    DeviceFactory.create(customer=customer, push_enabled=True)
    campaign = scheduled()
    other = scheduled()
    assert cancel(staff_client, campaign.pk).status_code == 200
    started = services.start_due_campaigns(AT + timedelta(minutes=1))
    assert started == [other.pk]
    campaign.refresh_from_db()
    assert campaign.state == "cancelled"
    assert not CampaignRecipient.objects.filter(campaign=campaign).exists()
    assert not OutboxMessage.objects.filter(
        topic="campaign.send_batch", payload__campaign_id=campaign.pk
    ).exists()
    # and once started, the other one can no longer be cancelled
    assert cancel(staff_client, other.pk).status_code == 409


def test_a_cancelled_campaign_no_longer_holds_its_segment(staff_client: APIClient, clock: Any) -> None:
    segment = CustomerSegmentFactory.create(name="Doimiy mijozlar")
    campaign = scheduled(
        audience_type="segment", audience_segment=segment, audience_segment_name=segment.name
    )
    assert staff_client.delete(f"/api/v1/admin/segments/{segment.pk}/").status_code == 409
    assert cancel(staff_client, campaign.pk).status_code == 200
    assert staff_client.delete(f"/api/v1/admin/segments/{segment.pk}/").status_code == 204
