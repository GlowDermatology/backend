"""Contract §2.8 ``POST notifications/{id}/cancel/`` (superuser): an approved campaign is cancelled before the
beat starts it — idempotent, audited, 409 for a draft or once the send started; the beat never starts a
cancelled campaign; a plain staff member gets 403."""

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
    ScheduledCampaignFactory,
    SendingCampaignFactory,
    SentCampaignFactory,
    TestedCampaignFactory,
)

pytestmark = pytest.mark.django_db

URL = "/api/v1/admin/notifications/"
AT = datetime(2026, 10, 27, 10, tzinfo=TASHKENT)


def scheduled(**overrides: Any) -> Campaign:
    return ScheduledCampaignFactory.create(scheduled_at=AT, dispatch_after=AT, **overrides)


def cancel(client: APIClient, campaign_id: int) -> Any:
    return client.post(f"{URL}{campaign_id}/cancel/", format="json")


def test_a_scheduled_campaign_is_cancelled_once_and_audited(
    superuser_client: APIClient, superuser: StaffUser, staff: StaffUser, clock: Any
) -> None:
    campaign = scheduled(author=staff)
    response = cancel(superuser_client, campaign.pk)
    assert response.status_code == 200, response.data
    assert response.data["id"] == campaign.pk and response.data["state"] == "cancelled"
    assert response.data["cancelled_at"] == "2026-10-24T12:05:00+05:00" == ANCHOR.isoformat()
    assert response.data["cancelled_by"] == {"id": superuser.pk, "display_name": "Egasi"}
    assert (response.data["sent_at"], response.data["sent"], response.data["failed"]) == (None, None, None)
    assert response.data["approved_at"] is not None and response.data["dispatch_after"] == AT.isoformat()
    campaign.refresh_from_db()
    assert (campaign.state, campaign.cancelled_at, campaign.cancelled_by) == ("cancelled", ANCHOR, superuser)
    entry = AuditEntry.objects.get(action="notification.cancelled", entity_id=str(campaign.pk))
    assert entry.actor_id == superuser.pk
    assert entry.before == {
        "state": "scheduled",
        "scheduled_at": AT.isoformat(),
        "dispatch_after": AT.isoformat(),
    }
    assert entry.after == {"state": "cancelled", "cancelled_at": ANCHOR.isoformat()}
    # idempotent: the same answer, no second audit row
    again = cancel(superuser_client, campaign.pk)
    assert again.status_code == 200 and again.data == response.data
    assert AuditEntry.objects.filter(action="notification.cancelled").count() == 1
    listed = superuser_client.get(URL, {"state": "cancelled"}).json()["results"]
    assert [row["id"] for row in listed] == [campaign.pk]
    assert superuser_client.get(URL, {"state": "scheduled"}).json()["results"] == []
    assert labels.NOTIFICATION_STATE["cancelled"] == "Bekor qilindi"


def test_a_plain_staff_member_cannot_cancel(staff_client: APIClient, clock: Any) -> None:
    campaign = scheduled()
    response = cancel(staff_client, campaign.pk)
    assert response.status_code == 403 and response.data["code"] == "permission_denied"
    campaign.refresh_from_db()
    assert campaign.state == "scheduled"


@pytest.mark.parametrize("state", [Campaign.State.DRAFT, Campaign.State.TESTED])
def test_a_draft_is_not_cancelled_but_deleted_or_edited(
    superuser_client: APIClient, state: str, clock: Any
) -> None:
    factory = TestedCampaignFactory if state == Campaign.State.TESTED else CampaignFactory
    campaign = factory.create()
    response = cancel(superuser_client, campaign.pk)
    assert response.status_code == 409
    assert (response.data["code"], response.data["message"]) == (
        "invalid_transition",
        services.MESSAGE_CANCEL_DRAFT,
    )
    campaign.refresh_from_db()
    assert campaign.state == state and campaign.cancelled_at is None


@pytest.mark.parametrize("state", [Campaign.State.SENDING, Campaign.State.STOPPING, Campaign.State.SENT])
def test_a_campaign_whose_send_started_cannot_be_cancelled(
    superuser_client: APIClient, state: str, clock: Any
) -> None:
    if state == Campaign.State.SENT:
        campaign = SentCampaignFactory.create()
    elif state == Campaign.State.STOPPING:
        campaign = SendingCampaignFactory.create(state=state, stop_requested_at=ANCHOR)
    else:
        campaign = SendingCampaignFactory.create(started_at=ANCHOR - timedelta(minutes=1))
    response = cancel(superuser_client, campaign.pk)
    assert response.status_code == 409
    assert (response.data["code"], response.data["message"]) == ("conflict", services.MESSAGE_CANCEL_STARTED)
    campaign.refresh_from_db()
    assert campaign.state == state and campaign.cancelled_at is None
    assert not AuditEntry.objects.filter(action="notification.cancelled").exists()


def test_unknown_campaign_is_404(superuser_client: APIClient) -> None:
    assert cancel(superuser_client, 999_999).status_code == 404


def test_the_beat_never_starts_a_cancelled_campaign(superuser_client: APIClient, clock: Any) -> None:
    customer = CustomerFactory.create()
    DeviceFactory.create(customer=customer, push_enabled=True)
    campaign = scheduled()
    other = scheduled()
    assert cancel(superuser_client, campaign.pk).status_code == 200
    started = services.start_due_campaigns(AT + timedelta(minutes=1))
    assert started == [other.pk]
    campaign.refresh_from_db()
    assert campaign.state == "cancelled"
    assert not CampaignRecipient.objects.filter(campaign=campaign).exists()
    assert not OutboxMessage.objects.filter(
        topic="campaign.send_batch", payload__campaign_id=campaign.pk
    ).exists()
    # and once started, the other one can no longer be cancelled
    assert cancel(superuser_client, other.pk).status_code == 409


def test_a_cancelled_campaign_no_longer_holds_its_segment(superuser_client: APIClient, clock: Any) -> None:
    segment = CustomerSegmentFactory.create(name="Doimiy mijozlar")
    campaign = scheduled(
        audience_type="segment", audience_segment=segment, audience_segment_name=segment.name
    )
    assert superuser_client.delete(f"/api/v1/admin/segments/{segment.pk}/").status_code == 409
    assert cancel(superuser_client, campaign.pk).status_code == 200
    assert superuser_client.delete(f"/api/v1/admin/segments/{segment.pk}/").status_code == 204
