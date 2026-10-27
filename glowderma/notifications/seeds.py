"""Demo notifications (design 03 §7.12): two segments, Malika as a manual member of both, four campaigns.

Templates come from the data migration (§3.1). Campaigns 1–3 are inserted directly in state ``sent`` with
the drawn counters (``recipient_count``/``sent``/``delivered`` are stored snapshots, §7.10 #9–10; no
``CampaignRecipient`` rows are materialised); ``campaign_state_fields`` requires ``failed`` and
``started_at`` in that state, so the seeder sets ``failed = 0`` and ``started_at = sent_at − 2 min``
(review minor 17b). Campaign 4 is ``scheduled`` for 26 October 10:00 and will be sent by beat.

Idempotent by natural keys: segment ``name``, member ``(segment, customer)``, campaign ``(title,
created_at)``. The author is the demo staff ``admin@glowderma.uz`` (NULL when missing), ``link_product``
is product 4 by ``billz_id`` (NULL when missing — the label snapshot keeps the link valid), Malika is looked
up by phone (skipped when missing). Every ``created_at``/``updated_at`` is pinned to a design instant.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from django.contrib.auth import get_user_model
from django.db import transaction

from glowderma.accounts.models import Customer, StaffUser
from glowderma.accounts.seeds import MALIKA_ID, NAMED_CUSTOMERS
from glowderma.catalog.models import Product
from glowderma.core import enums
from glowderma.core.seeding import SeedContext, pinned_clock, register_seeder
from glowderma.core.timeutils import TASHKENT
from glowderma.notifications.models import Campaign, CustomerSegment, SegmentMember

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
    sent_at: datetime | None  # None = still scheduled
    recipient_count: int
    sent: int | None
    delivered: int | None
    link_product: bool = False


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


def _seed_campaigns(segments: dict[int, CustomerSegment], admin: StaffUser | None) -> None:
    product = Product.objects.filter(billz_id=LINK_PRODUCT_BILLZ_ID).first()
    for spec in CAMPAIGNS:
        if Campaign.objects.filter(title=spec.title, created_at=spec.created_at).exists():
            continue
        segment = segments[spec.segment_id] if spec.segment_id is not None else None
        sent = spec.sent_at is not None
        with pinned_clock(spec.sent_at or spec.created_at):  # updated_at = the last transition
            Campaign.objects.create(
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
                state=Campaign.State.SENT if sent else Campaign.State.SCHEDULED,
                scheduled_at=spec.scheduled_at,
                started_at=spec.sent_at - SEND_LEAD if spec.sent_at is not None else None,
                sent_at=spec.sent_at,
                sent=spec.sent,
                failed=0 if sent else None,
                delivered=spec.delivered,
                author=admin,
                created_at=spec.created_at,
            )


@register_seeder("notifications", order=90)
def seed_notifications(context: SeedContext) -> None:
    with transaction.atomic():
        admin = get_user_model().objects.filter(email=SEED_ADMIN_EMAIL).first()
        segments = _seed_segments(admin)
        _seed_members(segments, admin)
        _seed_campaigns(segments, admin)
