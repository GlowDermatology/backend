"""Demo notifications (design 03 §7.12): two segments, Malika as a manual member of both, five campaigns.

Templates come from the data migration (§3.1). Campaigns 1–3 are inserted directly in state ``sent`` with
the drawn counters (``recipient_count``/``sent``/``delivered`` are stored snapshots, §7.10 #9–10; no
``CampaignRecipient`` rows are materialised); ``campaign_state_fields`` requires ``failed`` and
``started_at`` in that state, so the seeder sets ``failed = 0`` and ``started_at = sent_at − 2 min``
(review minor 17b). Campaign 4 is approved (``scheduled``) for 26 October 10:00 — tested and approved by
the demo superuser on 24 October, ``dispatch_after = scheduled_at`` — and will be sent by beat; when
Malika's pushable device exists it is registered as a test device and campaign 4 carries its confirmed
test. Campaign 5 is a ``draft`` so the demo shows the approval flow from the start.

Idempotent by natural keys: segment ``name``, member ``(segment, customer)``, campaign ``(title,
created_at)``, test device ``device``, test ``(campaign, revision)``. The author / approver is the demo
staff ``admin@glowderma.uz`` (NULL when missing), ``link_product`` is product 4 by ``billz_id`` (NULL when
missing — the label snapshot keeps the link valid), Malika is looked up by phone (skipped when missing).
Every ``created_at``/``updated_at`` is pinned to a design instant.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from django.contrib.auth import get_user_model
from django.db import transaction

from glowderma.accounts.models import Customer, Device, StaffUser
from glowderma.accounts.seeds import MALIKA_ID, NAMED_CUSTOMERS
from glowderma.catalog.models import Product
from glowderma.core import enums
from glowderma.core.seeding import SeedContext, pinned_clock, register_seeder
from glowderma.core.timeutils import TASHKENT
from glowderma.notifications.models import (
    Campaign,
    CampaignTest,
    CustomerSegment,
    SegmentMember,
    TestDevice,
)
from glowderma.notifications.selectors import PUSHABLE_DEVICE

SEED_ADMIN_EMAIL = "admin@glowderma.uz"
MALIKA_PHONE = next(phone for pk, _, phone in NAMED_CUSTOMERS if pk == MALIKA_ID)
SEGMENTS_CREATED_AT = datetime(2026, 10, 10, 10, 0, tzinfo=TASHKENT)
# id, name, min_online_orders, min_lifetime_amount
SEGMENTS: tuple[tuple[int, str, int | None, int | None], ...] = (
    (1, "Doimiy mijozlar", 3, None),
    (2, "10 mln+ xarid", None, 10_000_000),
)
LINK_PRODUCT_BILLZ_ID = "4607012345"  # product 4 of catalog.seed
LINK_PRODUCT_LABEL = "Seramidli namlantiruvchi krem"
SEND_LEAD = timedelta(minutes=2)  # started_at = sent_at − 2 min
# Campaign 4's approval trail (24 October): test pushed 11:38, confirmed 11:40, approved 11:45.
TEST_SENT_AT = datetime(2026, 10, 24, 11, 38, tzinfo=TASHKENT)
TEST_CONFIRMED_AT = datetime(2026, 10, 24, 11, 40, tzinfo=TASHKENT)
APPROVED_AT = datetime(2026, 10, 24, 11, 45, tzinfo=TASHKENT)
TEST_DEVICE_LABEL = "Malika — iPhone 14"


def _dt(month: int, day: int, hour: int, minute: int) -> datetime:
    return datetime(2026, month, day, hour, minute, tzinfo=TASHKENT)


@dataclass(frozen=True, slots=True)
class CampaignSpec:
    pk: int
    kind: str
    title: str
    body: str
    audience_type: str
    segment_id: int | None  # design segment id for audience ``segment``
    created_at: datetime
    scheduled_at: datetime | None
    sent_at: datetime | None  # None = not sent yet
    recipient_count: int
    sent: int | None
    delivered: int | None
    link_product: bool = False
    state: str = Campaign.State.SENT  # ``sent`` rows carry counters; ``scheduled`` = approved; ``draft``


CAMPAIGNS: tuple[CampaignSpec, ...] = (
    CampaignSpec(
        1,
        enums.NotificationKind.PROMO,
        "Quyoshdan himoya: 15% chegirma",
        "Barcha SPF kremlarga 15% chegirma — 25-oktabrgacha. Terini kuz quyoshidan ham himoyalang.",
        enums.AudienceType.SEGMENT,
        2,
        _dt(10, 18, 16, 30),
        None,
        _dt(10, 18, 16, 31),
        318,
        318,
        301,
    ),
    CampaignSpec(
        2,
        enums.NotificationKind.GENERAL,
        "Yangi mahsulotlar keldi",
        "Serumlar va tonerlar bo‘limida yangi mahsulotlar. Ilovada ko‘ring.",
        enums.AudienceType.ALL,
        None,
        _dt(10, 20, 15, 0),
        _dt(10, 21, 10, 0),
        _dt(10, 21, 10, 2),
        4740,
        4740,
        4205,
    ),
    CampaignSpec(
        3,
        enums.NotificationKind.PROMO,
        "Kuzgi namlik to‘plami",
        "Tanlangan namlantiruvchi kremlarga 20% chegirma — 30-noyabrgacha.",
        enums.AudienceType.ALL,
        None,
        _dt(10, 24, 9, 40),
        _dt(10, 24, 12, 0),
        _dt(10, 24, 12, 2),
        4751,
        4751,
        4388,
        link_product=True,
    ),
    CampaignSpec(
        4,
        enums.NotificationKind.GENERAL,
        "Yangi dermatolog qabuli ochildi",
        "Yangi dermatolog qabuliga yozilish ochildi. Ilova orqali qulay vaqtni band qiling.",
        enums.AudienceType.SEGMENT,
        1,
        _dt(10, 24, 11, 30),
        _dt(10, 26, 10, 0),
        None,
        612,
        None,
        None,
        state=Campaign.State.SCHEDULED,
    ),
    CampaignSpec(
        5,
        enums.NotificationKind.GENERAL,
        "Qish parvarishi bo‘yicha maslahatlar",
        "Sovuq kunlarda teri parvarishi: dermatologlarimizning 5 ta maslahati ilovada.",
        enums.AudienceType.ALL,
        None,
        _dt(10, 24, 12, 0),
        None,
        None,
        4751,
        None,
        None,
        state=Campaign.State.DRAFT,
    ),
)


def _seed_segments(admin: StaffUser | None) -> dict[int, CustomerSegment]:
    rows: dict[int, CustomerSegment] = {}
    for pk, name, min_orders, min_amount in SEGMENTS:
        segment = CustomerSegment.objects.filter(name=name).first()
        if segment is None:
            with pinned_clock(SEGMENTS_CREATED_AT):
                segment = CustomerSegment.objects.create(
                    id=pk,
                    name=name,
                    min_online_orders=min_orders,
                    min_lifetime_amount=min_amount,
                    created_by=admin,
                )
        rows[pk] = segment
    return rows


def _seed_members(segments: dict[int, CustomerSegment], admin: StaffUser | None) -> None:
    malika = Customer.objects.filter(phone=MALIKA_PHONE).first()
    if malika is None:
        return
    for segment in segments.values():
        with pinned_clock(SEGMENTS_CREATED_AT):
            SegmentMember.objects.get_or_create(
                segment=segment, customer=malika, defaults={"added_by": admin}
            )


def _approval_fields(spec: CampaignSpec, admin: StaffUser | None, tested: bool) -> dict[str, object]:
    """Campaign 4: approved for revision 1 at :data:`APPROVED_AT`, dispatched at its requested time; the
    test confirmation is recorded when a test device could be registered."""
    if spec.state != Campaign.State.SCHEDULED:
        return {}
    fields: dict[str, object] = {
        "approved_at": APPROVED_AT,
        "approved_by": admin,
        "approved_revision": 1,
        "dispatch_after": spec.scheduled_at,
    }
    if tested:
        fields.update(tested_revision=1, test_confirmed_at=TEST_CONFIRMED_AT, test_confirmed_by=admin)
    return fields


def _seed_test_device(admin: StaffUser | None) -> TestDevice | None:
    """Malika's pushable device as the demo test target (None when accounts did not seed it)."""
    device = (
        Device.objects.filter(PUSHABLE_DEVICE, customer__phone=MALIKA_PHONE)
        .order_by("-last_active_at", "-id")
        .first()
    )
    if device is None:
        return None
    test_device = TestDevice.objects.filter(device=device, is_active=True).first()
    if test_device is None:
        with pinned_clock(TEST_SENT_AT - timedelta(minutes=3)):
            test_device = TestDevice.objects.create(
                device=device, phone=MALIKA_PHONE, label=TEST_DEVICE_LABEL, registered_by=admin
            )
    return test_device


def _seed_test(campaign: Campaign, test_device: TestDevice | None, admin: StaffUser | None) -> None:
    if test_device is None or CampaignTest.objects.filter(campaign=campaign, revision=1).exists():
        return
    CampaignTest.objects.create(
        campaign=campaign,
        revision=1,
        test_device=test_device,
        device=test_device.device,
        device_label=test_device.label,
        phone=test_device.phone,
        sent_by=admin,
        sent_at=TEST_SENT_AT,
        fcm_ok=True,
        provider_message_id="seed-fake-campaign-4-test",
        confirmed_by=admin,
        confirmed_at=TEST_CONFIRMED_AT,
    )


def _seed_campaigns(
    segments: dict[int, CustomerSegment], admin: StaffUser | None, test_device: TestDevice | None
) -> None:
    product = Product.objects.filter(billz_id=LINK_PRODUCT_BILLZ_ID).first()
    for spec in CAMPAIGNS:
        existing = Campaign.objects.filter(title=spec.title, created_at=spec.created_at).first()
        if existing is not None:
            if spec.state == Campaign.State.SCHEDULED and existing.tested_revision is not None:
                _seed_test(existing, test_device, admin)
            continue
        segment = segments[spec.segment_id] if spec.segment_id is not None else None
        sent = spec.state == Campaign.State.SENT
        approved = spec.state == Campaign.State.SCHEDULED
        last_transition = spec.sent_at or (APPROVED_AT if approved else spec.created_at)
        with pinned_clock(last_transition):  # updated_at = the last transition
            campaign = Campaign.objects.create(
                id=spec.pk,
                kind=spec.kind,
                title=spec.title,
                body=spec.body,
                link_kind=enums.LinkKind.PRODUCT if spec.link_product else enums.LinkKind.NONE,
                link_product=product if spec.link_product else None,
                link_product_label=LINK_PRODUCT_LABEL if spec.link_product else "",
                audience_type=spec.audience_type,
                audience_segment=segment,
                audience_segment_name=segment.name if segment is not None else "",
                recipient_count=spec.recipient_count,
                state=spec.state,
                scheduled_at=spec.scheduled_at,
                started_at=spec.sent_at - SEND_LEAD if spec.sent_at is not None else None,
                sent_at=spec.sent_at,
                sent=spec.sent,
                failed=0 if sent else None,
                delivered=spec.delivered,
                author=admin,
                created_at=spec.created_at,
                **_approval_fields(spec, admin, tested=test_device is not None),
            )
        if approved:
            _seed_test(campaign, test_device, admin)


@register_seeder("notifications", order=90)
def seed_notifications(context: SeedContext) -> None:
    with transaction.atomic():
        admin = get_user_model().objects.filter(email=SEED_ADMIN_EMAIL).first()
        segments = _seed_segments(admin)
        _seed_members(segments, admin)
        _seed_campaigns(segments, admin, _seed_test_device(admin))
