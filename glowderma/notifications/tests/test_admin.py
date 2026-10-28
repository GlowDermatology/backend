"""Design 03 §8: the notifications models are registered read-only with the design columns."""

import pytest
from django.contrib import admin
from django.db import models
from django.test import Client, RequestFactory
from django.urls import reverse

from glowderma.accounts.tests.factories import StaffUserFactory
from glowderma.core.admin import ReadOnlyAdmin
from glowderma.notifications.admin import segment_rules
from glowderma.notifications.models import (
    Campaign,
    CampaignRecipient,
    CustomerSegment,
    InboxNotification,
    SegmentMember,
    StatusTemplate,
)
from glowderma.notifications.tests.factories import (
    CampaignRecipientFactory,
    CustomerSegmentFactory,
    InboxNotificationFactory,
    SegmentMemberFactory,
    SentCampaignFactory,
)

pytestmark = pytest.mark.django_db

MODELS = [StatusTemplate, CustomerSegment, SegmentMember, Campaign, CampaignRecipient, InboxNotification]


@pytest.mark.parametrize("model", MODELS)
def test_registered_read_only(model: type[models.Model]) -> None:
    model_admin = admin.site._registry[model]
    assert isinstance(model_admin, ReadOnlyAdmin)
    assert model_admin.list_display != ("__str__",) and model_admin.search_fields
    request = RequestFactory().get("/django-admin/")
    request.user = StaffUserFactory.create(is_staff=True)
    assert model_admin.has_view_permission(request)
    assert not model_admin.has_add_permission(request)
    assert not model_admin.has_change_permission(request)
    assert not model_admin.has_delete_permission(request)
    assert model_admin.check() == []


def test_design_columns() -> None:
    assert admin.site._registry[Campaign].list_display == (
        "id",
        "kind",
        "title",
        "audience_type",
        "state",
        "scheduled_at",
        "sent_at",
        "sent",
        "failed",
    )
    assert admin.site._registry[Campaign].list_filter == ("kind", "state", "audience_type")
    assert admin.site._registry[InboxNotification].list_filter == ("kind", "push_state", "source_type")
    assert admin.site._registry[StatusTemplate].list_display == (
        "status",
        "text_uz",
        "updated_at",
        "updated_by",
    )
    assert admin.site._registry[CustomerSegment].list_display == ("id", "name", segment_rules, "created_at")
    assert segment_rules(CustomerSegmentFactory.build(min_online_orders=3, min_lifetime_amount=None)) == (
        "orders ≥ 3 · lifetime ≥ null"
    )


def test_changelists_change_pages_and_search_render() -> None:
    client = Client()
    client.force_login(StaffUserFactory.create(is_staff=True, is_superuser=True))
    member = SegmentMemberFactory.create()
    recipient = CampaignRecipientFactory.create(campaign=SentCampaignFactory.create(title="Kuzgi namlik"))
    rows = [
        StatusTemplate.objects.get(status="processing"),
        member.segment,
        member,
        recipient.campaign,
        recipient,
        InboxNotificationFactory.create(),
    ]
    for row in rows:
        meta = row._meta
        assert client.get(reverse(f"admin:{meta.app_label}_{meta.model_name}_changelist")).status_code == 200
        change = client.get(reverse(f"admin:{meta.app_label}_{meta.model_name}_change", args=[row.pk]))
        assert change.status_code == 200
        assert client.post(reverse(f"admin:{meta.app_label}_{meta.model_name}_add"), {}).status_code == 403
    search = client.get(reverse("admin:notifications_campaign_changelist"), {"q": "Kuzgi"})
    assert search.status_code == 200 and b"Kuzgi namlik" in search.content
    by_phone = client.get(
        reverse("admin:notifications_segmentmember_changelist"), {"q": str(member.customer.phone)}
    )
    assert by_phone.status_code == 200 and str(member.customer.phone).encode() in by_phone.content
