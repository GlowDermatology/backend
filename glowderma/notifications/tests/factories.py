"""factory-boy factories for the notifications models (call them as ``XFactory.create(...)``).

``StatusTemplateFactory`` reuses the migration row of its ``status`` (the column is unique and the four
rows always exist); the others create fresh rows with unique natural keys.
"""

from datetime import datetime
from typing import Any

from factory.declarations import LazyAttribute, Sequence, SubFactory
from factory.django import DjangoModelFactory

from glowderma.accounts.tests.factories import CustomerFactory, DeviceFactory
from glowderma.core import enums
from glowderma.core.timeutils import TASHKENT
from glowderma.notifications.models import (
    Campaign,
    CampaignRecipient,
    CustomerSegment,
    InboxNotification,
    SegmentMember,
    StatusTemplate,
)


class StatusTemplateFactory(DjangoModelFactory[StatusTemplate]):
    class Meta:
        model = StatusTemplate
        django_get_or_create = ("status",)

    status = enums.TemplateStatus.PROCESSING
    text_uz = "To‘lov qabul qilindi. Buyurtma №{raqam} tayyorlanmoqda."


class CustomerSegmentFactory(DjangoModelFactory[CustomerSegment]):
    class Meta:
        model = CustomerSegment

    name = Sequence(lambda n: f"Guruh {n + 1}")  # type: ignore[no-untyped-call]
    min_online_orders = 3


class SegmentMemberFactory(DjangoModelFactory[SegmentMember]):
    class Meta:
        model = SegmentMember

    segment = SubFactory(CustomerSegmentFactory)  # type: ignore[no-untyped-call]
    customer = SubFactory(CustomerFactory)  # type: ignore[no-untyped-call]


class CampaignFactory(DjangoModelFactory[Campaign]):
    class Meta:
        model = Campaign

    kind = enums.NotificationKind.GENERAL
    title = Sequence(lambda n: f"Xabar {n + 1}")  # type: ignore[no-untyped-call]
    body = "Yangi mahsulotlar va aksiyalar haqida bilib oling."
    audience_type = enums.AudienceType.ALL
    state = Campaign.State.QUEUED


class SentCampaignFactory(CampaignFactory):
    """A campaign in state ``sent`` with the counters ``campaign_state_fields`` requires."""

    state = Campaign.State.SENT
    started_at = datetime(2026, 10, 24, 12, 0, tzinfo=TASHKENT)
    sent_at = datetime(2026, 10, 24, 12, 2, tzinfo=TASHKENT)
    recipient_count = 10
    sent = 10
    failed = 0


class CampaignRecipientFactory(DjangoModelFactory[CampaignRecipient]):
    class Meta:
        model = CampaignRecipient

    campaign = SubFactory(CampaignFactory)  # type: ignore[no-untyped-call]
    customer = SubFactory(CustomerFactory)  # type: ignore[no-untyped-call]
    device = LazyAttribute(lambda o: DeviceFactory.create(customer=o.customer))  # type: ignore[no-untyped-call]


def _campaign_link(recipient: Any) -> dict[str, Any]:
    return {"kind": "none"}


class InboxNotificationFactory(DjangoModelFactory[InboxNotification]):
    class Meta:
        model = InboxNotification

    customer = SubFactory(CustomerFactory)  # type: ignore[no-untyped-call]
    kind = InboxNotification.Kind.GENERAL
    title = "Yangi mahsulotlar keldi"
    body = "Yangi mahsulotlar va aksiyalar haqida bilib oling."
    link = LazyAttribute(_campaign_link)  # type: ignore[no-untyped-call]
    source_type = InboxNotification.SourceType.CAMPAIGN
    source_id = 1
    language = enums.ContentLanguage.UZ
    dedupe_key = Sequence(lambda n: f"campaign:1:{n + 1}")  # type: ignore[no-untyped-call]
