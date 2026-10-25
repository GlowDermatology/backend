"""Read-side helpers of `notifications` (design 03 §3.2 membership, §6.5 meta and segment sizes).

The segment membership predicate is implemented once here (:func:`segment_members`) and reused by the
campaign audience, the meta sizes, the members list and — through :func:`segments_for_customer` and
``customers/?segment=`` — the customers slice.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any, TypedDict

from django.db.models import (
    BigIntegerField,
    Count,
    Exists,
    Expression,
    IntegerField,
    OuterRef,
    Q,
    QuerySet,
    Subquery,
    Sum,
    Value,
)
from django.db.models.functions import Coalesce, Collate

from glowderma.accounts.models import Customer, Device
from glowderma.catalog.models import Product
from glowderma.catalog.selectors import DISPLAY_NAME
from glowderma.core import enums
from glowderma.loyalty.models import Purchase
from glowderma.notifications.models import Campaign, CustomerSegment, SegmentMember, StatusTemplate

PUSHABLE_DEVICE = Q(is_active=True, push_enabled=True) & ~Q(fcm_token="")


# --- rule expressions (design 03 §3.2 / §6.3: the same subqueries the customers list uses) -----------------


def online_orders_expression() -> Expression:
    """``online_orders_count`` of the customer row being annotated: paid, non-reversed online orders — a
    ``loyalty.Purchase`` row of source ``online`` exists only once the order was paid (design 03 §3.2)."""
    purchases = (
        Purchase.objects.filter(
            customer_id=OuterRef("pk"), source=enums.PurchaseSource.ONLINE, reversed=False
        )
        .order_by()
        .values("customer_id")
        .annotate(count=Count("id"))
        .values("count")
    )
    return Coalesce(Subquery(purchases, output_field=IntegerField()), Value(0), output_field=IntegerField())


def lifetime_total_expression() -> Expression:
    """``lifetime_total`` (so'm) of the customer row being annotated: Σ ``amount`` of the non-reversed
    purchases of both channels — the card's ``totals.lifetime``."""
    totals = (
        Purchase.objects.filter(customer_id=OuterRef("pk"), reversed=False)
        .order_by()
        .values("customer_id")
        .annotate(total=Sum("amount"))
        .values("total")
    )
    return Coalesce(
        Subquery(totals, output_field=BigIntegerField()), Value(0), output_field=BigIntegerField()
    )


def lifetime_totals(customer_ids: Iterable[int]) -> dict[int, int]:
    """``{customer_id: lifetime_total}`` for the given customers (the customers slice reuses this)."""
    rows = (
        Customer.objects.filter(pk__in=list(customer_ids))
        .annotate(lifetime_total=lifetime_total_expression())
        .values_list("pk", "lifetime_total")
    )
    return {int(pk): int(total or 0) for pk, total in rows}


def annotate_customer_stats(qs: QuerySet[Customer]) -> QuerySet[Customer]:
    return qs.annotate(
        online_orders_count=online_orders_expression(), lifetime_total=lifetime_total_expression()
    )


# --- membership (contract §2.8) -----------------------------------------------------------------------------


def rules_set(segment: CustomerSegment) -> bool:
    return segment.min_online_orders is not None or segment.min_lifetime_amount is not None


def rule_match_q(segment: CustomerSegment) -> Q:
    """The ANDed rule predicate over an ``annotate_customer_stats`` queryset; never matches without rules."""
    if not rules_set(segment):
        return Q(pk__in=[])
    condition = Q()
    if segment.min_online_orders is not None:
        condition &= Q(online_orders_count__gte=segment.min_online_orders)
    if segment.min_lifetime_amount is not None:
        condition &= Q(lifetime_total__gte=segment.min_lifetime_amount)
    return condition


def manual_member_q(segment: CustomerSegment) -> Q:
    return Q(Exists(SegmentMember.objects.filter(segment=segment, customer_id=OuterRef("pk"))))


def segment_members(segment: CustomerSegment) -> QuerySet[Customer]:
    """active customer ∧ (all set rules match ∨ manual member); with no rules, manual only."""
    qs = annotate_customer_stats(Customer.objects.filter(status=enums.CustomerStatus.ACTIVE))
    return qs.filter(manual_member_q(segment) | rule_match_q(segment))


def segment_member_ids(segment: CustomerSegment) -> QuerySet[Customer, int]:
    return segment_members(segment).order_by().values_list("pk", flat=True)


def matches_rules(segment: CustomerSegment, customer_id: int) -> bool:
    """Whether the (active) customer is a member through the rules alone."""
    qs = annotate_customer_stats(Customer.objects.filter(pk=customer_id, status=enums.CustomerStatus.ACTIVE))
    return qs.filter(rule_match_q(segment)).exists()


def segments_for_customer(customer_id: int) -> list[CustomerSegment]:
    """Every segment the customer belongs to, by id — two queries whatever the number of segments.

    The customer's stats come from one annotated row (the same expressions as ``segment_members``); each
    segment then carries whether the customer is a manual member, and the rules are checked in Python
    (``rules_hold``). A non-active customer belongs to none.
    """
    customer = annotate_customer_stats(
        Customer.objects.filter(pk=customer_id, status=enums.CustomerStatus.ACTIVE)
    ).first()
    if customer is None:
        return []
    online_orders_count = int(getattr(customer, "online_orders_count", 0))
    lifetime = int(getattr(customer, "lifetime_total", 0))
    segments = CustomerSegment.objects.order_by("id").annotate(
        is_manual_member=Exists(
            SegmentMember.objects.filter(segment_id=OuterRef("pk"), customer_id=customer_id)
        )
    )
    return [
        segment
        for segment in segments
        if getattr(segment, "is_manual_member", False)
        or rules_hold(segment, online_orders_count=online_orders_count, lifetime=lifetime)
    ]


def rules_hold(segment: CustomerSegment, *, online_orders_count: int, lifetime: int) -> bool:
    """The segment's set rules all hold for these stats (never without rules): the Python mirror of
    ``rule_match_q`` for one customer whose stats are already known."""
    if not rules_set(segment):
        return False
    if segment.min_online_orders is not None and online_orders_count < segment.min_online_orders:
        return False
    return segment.min_lifetime_amount is None or lifetime >= segment.min_lifetime_amount


class SegmentStats(TypedDict):
    manual_member_count: int
    member_count: int
    size: int


def has_pushable_device() -> Exists:
    """Over a ``Customer`` queryset: the customer has at least one pushable device."""
    return Exists(Device.objects.filter(PUSHABLE_DEVICE, customer_id=OuterRef("pk")))


def segment_stats(segment: CustomerSegment) -> SegmentStats:
    """``manual_member_count``, ``member_count`` (predicate) and ``size``.

    ``size`` counts the members a campaign can reach — members with at least one pushable device — because
    a campaign pushes to one device per customer (``CampaignRecipient`` is unique on ``(campaign, customer)``,
    :func:`audience_recipients`), so the meta size equals the ``recipient_count`` of a campaign sent to the
    segment.
    """
    counts = (
        segment_members(segment)
        .annotate(reachable=has_pushable_device())
        .aggregate(
            member_count=Count("pk", distinct=True),
            size=Count("pk", distinct=True, filter=Q(reachable=True)),
        )
    )
    return {
        "manual_member_count": SegmentMember.objects.filter(segment=segment).count(),
        "member_count": int(counts["member_count"] or 0),
        "size": int(counts["size"] or 0),
    }


def segment_size(segment: CustomerSegment) -> int:
    return segment_stats(segment)["size"]


# --- devices and audiences ----------------------------------------------------------------------------------


def pushable_devices() -> QuerySet[Device]:
    """Push-enabled active devices (with a token) of active customers."""
    return Device.objects.filter(PUSHABLE_DEVICE, customer__status=enums.CustomerStatus.ACTIVE)


def total_devices() -> int:
    """``meta.total_devices``: the active customers with at least one pushable device — the reach of an
    ``all`` campaign (one device per customer, like :func:`audience_recipients` and the segment ``size``)."""
    return Customer.objects.filter(has_pushable_device(), status=enums.CustomerStatus.ACTIVE).count()


def customer_push_device(customer_id: int) -> Device | None:
    """The device a status push goes to: the customer's most recently active pushable device."""
    return (
        Device.objects.filter(PUSHABLE_DEVICE, customer_id=customer_id)
        .order_by("-last_active_at", "-id")
        .first()
    )


def audience_recipients(campaign: Campaign) -> list[tuple[int, int]]:
    """``(customer_id, device_id)`` per customer of the campaign's audience — one (the latest) device each."""
    devices = pushable_devices()
    if campaign.audience_type == enums.AudienceType.SEGMENT:
        if campaign.audience_segment_id is None:
            return []
        devices = devices.filter(customer_id__in=segment_member_ids(campaign.audience_segment))  # type: ignore[arg-type]
    elif campaign.audience_type == enums.AudienceType.CUSTOMER:
        if campaign.audience_customer_id is None:
            return []
        devices = devices.filter(customer_id=campaign.audience_customer_id)
    latest: dict[int, int] = {}
    for customer_id, device_id in devices.order_by("customer_id", "last_active_at", "id").values_list(
        "customer_id", "id"
    ):
        latest[customer_id] = device_id  # the last row per customer is the most recently active one
    return sorted(latest.items())


# --- campaigns, templates, meta -----------------------------------------------------------------------------


def campaign_queryset() -> QuerySet[Campaign]:
    return Campaign.objects.select_related("author", "audience_segment", "link_product")


def filter_campaigns(qs: QuerySet[Campaign], *, state: str | None, kind: str | None) -> QuerySet[Campaign]:
    """Contract §2.8 filters; the API ``queued`` covers the internal ``queued`` and ``sending``."""
    if state == enums.NotificationState.QUEUED:
        qs = qs.filter(state__in=[Campaign.State.QUEUED, Campaign.State.SENDING])
    elif state is not None:
        qs = qs.filter(state=state)
    if kind is not None:
        qs = qs.filter(kind=kind)
    return qs


def get_campaign(pk: int) -> Campaign | None:
    return campaign_queryset().filter(pk=pk).first()


def templates() -> list[StatusTemplate]:
    """The four rows in ``TemplateStatus`` order."""
    order = {status: index for index, status in enumerate(enums.TemplateStatus.values)}
    rows = list(StatusTemplate.objects.select_related("updated_by"))
    return sorted(rows, key=lambda row: order.get(row.status, len(order)))


def get_template(status: str) -> StatusTemplate | None:
    if status not in enums.TemplateStatus.values:
        return None
    return StatusTemplate.objects.select_related("updated_by").filter(status=status).first()


def link_options() -> list[dict[str, Any]]:
    """Active products as ``{product_id, label}`` (uz display name), sorted by label (``glowderma_ci``)."""
    rows = (
        Product.objects.filter(status=enums.ProductStatus.ACTIVE)
        .annotate(label=DISPLAY_NAME, label_ci=Collate(DISPLAY_NAME, "glowderma_ci"))
        .order_by("label_ci", "id")
        .values_list("id", "label")
    )
    return [{"product_id": pk, "label": label} for pk, label in rows]


def meta() -> dict[str, Any]:
    """``GET notifications/meta/`` (contract §2.8)."""
    segments = [
        {"id": segment.pk, "name": segment.name, "size": segment_size(segment)}
        for segment in CustomerSegment.objects.order_by("id")
    ]
    return {
        "total_devices": total_devices(),
        "segments": segments,
        "templates": templates(),
        "link_options": link_options(),
    }
