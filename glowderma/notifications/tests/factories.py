"""factory-boy factories for the notifications models (call them as ``XFactory.create(...)``).

``StatusTemplateFactory`` reuses the migration row of its ``status`` (the column is unique and the four
rows always exist); the others create fresh rows with unique natural keys.
"""

from datetime import datetime, timedelta
from typing import Any

from factory.declarations import LazyAttribute, Sequence, SubFactory
from factory.django import DjangoModelFactory

from glowderma.accounts.tests.factories import CustomerFactory, DeviceFactory
from glowderma.core import enums
from glowderma.core.timeutils import TASHKENT
from glowderma.notifications.models import (
    Campaign,
    CampaignRecipient,
    CampaignTest,
    CustomerSegment,
    InboxNotification,
    SegmentMember,
    StatusTemplate,
    TestDevice,
)

NOW = datetime(2026, 10, 24, 12, 5, tzinfo=TASHKENT)


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
    state = Campaign.State.DRAFT


class TestedCampaignFactory(CampaignFactory):
    """A campaign whose current revision has a confirmed test (``campaign_state_fields``: ``tested`` needs
    ``tested_revision == content_revision`` and ``test_confirmed_at``)."""

    __test__ = False
    state = Campaign.State.TESTED
    tested_revision = 1
    test_confirmed_at = NOW - timedelta(minutes=10)


class ScheduledCampaignFactory(CampaignFactory):
    """An approved campaign waiting for ``dispatch_after`` (revision 1 tested and approved)."""

    state = Campaign.State.SCHEDULED
    tested_revision = 1
    test_confirmed_at = NOW - timedelta(minutes=10)
    approved_at = NOW - timedelta(minutes=5)
    approved_revision = 1
    dispatch_after = NOW


class SendingCampaignFactory(CampaignFactory):
    """A campaign the beat started (recipients are the test's business)."""

    state = Campaign.State.SENDING
    started_at = NOW
    sent = 0
    failed = 0


class SentCampaignFactory(CampaignFactory):
    """A campaign in state ``sent`` with the counters ``campaign_state_fields`` requires."""

    state = Campaign.State.SENT
    started_at = datetime(2026, 10, 24, 12, 0, tzinfo=TASHKENT)
    sent_at = datetime(2026, 10, 24, 12, 2, tzinfo=TASHKENT)
    recipient_count = 10
    sent = 10
    failed = 0


class TestDeviceFactory(DjangoModelFactory[TestDevice]):
    __test__ = False

    class Meta:
        model = TestDevice

    device = SubFactory(DeviceFactory)  # type: ignore[no-untyped-call]
    phone = LazyAttribute(lambda o: o.device.customer.phone)  # type: ignore[no-untyped-call]
    label = Sequence(lambda n: f"Test qurilmasi {n + 1}")  # type: ignore[no-untyped-call]


class CampaignTestFactory(DjangoModelFactory[CampaignTest]):
    """A delivered (``fcm_ok``) test of the campaign's current revision."""

    class Meta:
        model = CampaignTest

    campaign = SubFactory(CampaignFactory)  # type: ignore[no-untyped-call]
    revision = LazyAttribute(lambda o: o.campaign.content_revision)  # type: ignore[no-untyped-call]
    test_device = SubFactory(TestDeviceFactory)  # type: ignore[no-untyped-call]
    device = LazyAttribute(lambda o: o.test_device.device)  # type: ignore[no-untyped-call]
    device_label = LazyAttribute(lambda o: o.test_device.label)  # type: ignore[no-untyped-call]
    phone = LazyAttribute(lambda o: o.test_device.phone)  # type: ignore[no-untyped-call]
    fcm_ok = True
    provider_message_id = "fake-test-0"


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
