"""Read side of `customers` (contract §2.6, design 03 §6.3): the admin customers list and the customer card.

`customers` owns no tables (decision T2): it sits above `loyalty`, `commerce`, `appointments` and
`notifications` and aggregates them. Every loyalty number comes from ``loyalty.selectors``: the list orders
by the SQL annotations of :func:`loyalty.selectors.list_annotations` (so sorting and paging happen in the
database), the card reads ``customer_totals`` / ``account_summary`` / ``tier_for`` / ``ledger_for_customer`` /
``purchases_for_customer``; the list's segment filter reuses ``notifications.selectors.segment_member_ids``,
the card's groups evaluate the same membership rule in one query (:func:`customer_groups`), and the upcoming
appointments come from ``appointments.selectors.upcoming_count``.

Three reads have no selector in their owning module yet and are implemented here against the models
(reported to the lead): the customer row itself (``accounts`` has no selectors module), the active device
(design 01 §1.2 ``accounts.selectors.primary_device``) and the newest order of a customer (``commerce`` has no
selectors module yet).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime

from django.db.models import Exists, F, OrderBy, OuterRef, Q, QuerySet
from django.db.models.functions import Collate, Lower
from django.db.models.lookups import Contains

from glowderma.accounts.models import Customer, Device
from glowderma.appointments import selectors as appointments_selectors
from glowderma.commerce.models import Order
from glowderma.core import enums
from glowderma.loyalty import selectors as loyalty_selectors
from glowderma.loyalty.selectors import AccountSummary, LedgerRow, PurchaseRow, TierInfo, Totals
from glowderma.notifications import selectors as notifications_selectors
from glowderma.notifications.models import CustomerSegment, SegmentMember

NAME_COLLATION = "glowderma_ci"  # ICU case-insensitive collation (core.0007), used in ORDER BY only
SEARCH_MAX_LENGTH = 64
PHONE_SEARCH_MIN_DIGITS = 3
DEFAULT_ORDERING = "-registered_at"


def _name_ci() -> Collate:
    return Collate(F("name"), NAME_COLLATION)


# ``ordering`` value → ORDER BY terms; ties by ``id`` in the same direction (design 03 §6.3).
ORDERINGS: dict[str, tuple[OrderBy, ...]] = {
    "-registered_at": (F("registered_at").desc(), F("id").desc()),
    "registered_at": (F("registered_at").asc(), F("id").asc()),
    "name": (_name_ci().asc(), F("id").asc()),
    "-name": (_name_ci().desc(), F("id").desc()),
    "lifetime_total": (F("lifetime_total").asc(), F("id").asc()),
    "-lifetime_total": (F("lifetime_total").desc(), F("id").desc()),
    "balance": (F("balance").asc(), F("id").asc()),
    "-balance": (F("balance").desc(), F("id").desc()),
}


# --- the list (contract §2.6 ``GET customers/``) -------------------------------------------------------


def search_q(term: str) -> Q | None:
    """``name`` contains the term (case-insensitive, ``LOWER(name) LIKE …`` — the trigram index), or, when the
    term carries at least three digits, the phone contains those digits. Blank → no filter."""
    text = term.strip()
    if not text:
        return None
    condition = Q(Contains(Lower(F("name")), text.lower()))
    digits = re.sub(r"\D", "", text)
    if len(digits) >= PHONE_SEARCH_MIN_DIGITS:
        condition |= Q(phone__contains=digits)
    return condition


def customers_list(
    *,
    search: str | None = None,
    status: str | None = None,
    segment_id: int | None = None,
    ordering: str = DEFAULT_ORDERING,
) -> QuerySet[Customer]:
    """The filtered, ordered customer rows annotated with ``lifetime_total``, ``balance``,
    ``online_orders_count`` and ``percent`` (the active policy's rate for ``lifetime_total`` | ``None``)."""
    if ordering not in ORDERINGS:
        raise ValueError(f"Unknown customers ordering {ordering!r}")
    qs = Customer.objects.annotate(**loyalty_selectors.list_annotations())
    if status is not None:
        qs = qs.filter(status=status)
    if search is not None:
        condition = search_q(search)
        if condition is not None:
            qs = qs.filter(condition)
    if segment_id is not None:
        segment = CustomerSegment.objects.filter(pk=segment_id).first()
        if segment is None:
            return qs.none()
        qs = qs.filter(pk__in=notifications_selectors.segment_member_ids(segment))
    return qs.order_by(*ORDERINGS[ordering])


# --- the card (contract §2.6 ``GET customers/{id}/``) --------------------------------------------------


@dataclass(frozen=True, slots=True)
class OnlineOrders:
    count: int  # paid, non-reversed online purchases (what the loyalty programme counts)
    last_order_id: int | None  # the customer's newest order of any status (contract §7 item 18)
    last_order_at: datetime | None


@dataclass(frozen=True, slots=True)
class Group:
    id: int
    name: str


@dataclass(frozen=True, slots=True)
class DeviceInfo:
    model: str
    os: str
    app_version: str
    last_active_at: datetime
    push_enabled: bool


@dataclass(frozen=True, slots=True)
class CustomerCard:
    """Contract §2.6 ``CustomerCard``."""

    id: int
    name: str
    phone: str
    status: str
    registered_at: datetime
    deleted_at: datetime | None
    language: str
    address: str | None
    totals: Totals
    online_orders: OnlineOrders
    tier: TierInfo | None
    balance: int
    held: int
    available: int
    groups: list[Group]
    upcoming_appointments: int
    device: DeviceInfo | None
    ledger: list[LedgerRow]
    purchases: list[PurchaseRow]


def get_customer(customer_id: int) -> Customer | None:
    """The customer row of any status (deleted customers stay addressable), or ``None``."""
    return Customer.objects.filter(pk=customer_id).first()


def customer_exists(customer_id: int) -> bool:
    return Customer.objects.filter(pk=customer_id).exists()


def newest_order(customer_id: int) -> tuple[int, datetime] | None:
    """``(id, created_at)`` of the customer's newest order of any status (``created_at desc, id desc``)."""
    row = (
        Order.objects.filter(customer_id=customer_id)
        .order_by("-created_at", "-id")
        .values_list("id", "created_at")
        .first()
    )
    return None if row is None else (int(row[0]), row[1])


def active_device(customer_id: int) -> DeviceInfo | None:
    """The customer's active device (the most recently active one if several), or ``None``."""
    device = (
        Device.objects.filter(customer_id=customer_id, is_active=True)
        .order_by("-last_active_at", "-id")
        .only("model", "os", "app_version", "last_active_at", "push_enabled")
        .first()
    )
    if device is None:
        return None
    return DeviceInfo(
        model=device.model,
        os=device.os,
        app_version=device.app_version,
        last_active_at=device.last_active_at,
        push_enabled=device.push_enabled,
    )


def _rules_hold(segment: CustomerSegment, totals: Totals) -> bool:
    """The segment's set rules all hold for these stats (never without rules) — the Python mirror of
    ``notifications.selectors.rule_match_q`` over the card's ``customer_totals`` row, whose
    ``online_orders_count`` / ``lifetime`` are the same quantities as ``online_orders_expression`` /
    ``lifetime_total_expression`` (non-reversed online purchases; Σ non-reversed purchase amounts)."""
    if not notifications_selectors.rules_set(segment):
        return False
    if segment.min_online_orders is not None and totals.online_orders_count < segment.min_online_orders:
        return False
    return segment.min_lifetime_amount is None or totals.lifetime >= segment.min_lifetime_amount


def customer_groups(customer: Customer, totals: Totals) -> list[Group]:
    """The segments the customer belongs to (rules or manual membership, contract §2.8), by id; a deleted
    customer has none (membership requires an active customer).

    One query whatever the number of segments (design 03 §6.3, no N+1): every segment annotated with whether
    the customer is a manual member, the rules checked in Python against the card's ``totals``.
    ``notifications.selectors.segments_for_customer`` answers the same set with one query per segment, so
    the card does not call it (reported to the lead).
    """
    if customer.status != enums.CustomerStatus.ACTIVE:
        return []
    segments = (
        CustomerSegment.objects.order_by("id")
        .only("id", "name", "min_online_orders", "min_lifetime_amount")
        .annotate(
            is_manual_member=Exists(
                SegmentMember.objects.filter(segment_id=OuterRef("pk"), customer_id=customer.pk)
            )
        )
    )
    return [
        Group(id=segment.pk, name=segment.name)
        for segment in segments
        if getattr(segment, "is_manual_member", False) or _rules_hold(segment, totals)
    ]


def customer_card(customer_id: int) -> CustomerCard | None:
    """The full card of a customer of any status, or ``None`` when the id is unknown."""
    customer = get_customer(customer_id)
    if customer is None:
        return None
    totals = loyalty_selectors.customer_totals(customer.pk)
    summary: AccountSummary = loyalty_selectors.account_summary(customer.pk)
    last = newest_order(customer.pk)
    return CustomerCard(
        id=customer.pk,
        name=customer.name,
        phone=customer.phone,
        status=customer.status,
        registered_at=customer.registered_at,
        deleted_at=customer.deleted_at,
        language=customer.language,
        address=customer.address_text or None,
        totals=totals,
        online_orders=OnlineOrders(
            count=totals.online_orders_count,
            last_order_id=None if last is None else last[0],
            last_order_at=None if last is None else last[1],
        ),
        tier=loyalty_selectors.tier_for(totals.lifetime),
        balance=summary.balance,
        held=summary.held,
        available=summary.available,
        groups=customer_groups(customer, totals),
        upcoming_appointments=appointments_selectors.upcoming_count(customer.pk),
        device=active_device(customer.pk),
        ledger=loyalty_selectors.ledger_for_customer(customer.pk),
        purchases=loyalty_selectors.purchases_for_customer(customer.pk),
    )


def ledger_entry(entry_id: int) -> LedgerRow:
    """One ledger entry in the §2.6 shape (the adjustment response)."""
    return loyalty_selectors.ledger_entry(entry_id)


__all__ = [
    "DEFAULT_ORDERING",
    "ORDERINGS",
    "CustomerCard",
    "DeviceInfo",
    "Group",
    "OnlineOrders",
    "active_device",
    "customer_card",
    "customer_exists",
    "customer_groups",
    "customers_list",
    "get_customer",
    "ledger_entry",
    "newest_order",
    "search_q",
]
