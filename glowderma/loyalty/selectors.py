"""Read side of `loyalty` (design 02 §4, §1.1 derived policy state; contract §2.6 / §2.7 shapes).

Everything here is a read: policy versions and their derived state, the customer card numbers (totals,
balance/held/available, tier and next tier), the ledger and purchase rows in the contract's §2.6 shape, the
per-customer lifetime totals for segment rules and the ``Subquery`` annotations the customers list orders by.
Rows are returned as frozen dataclasses with the contract's field names (a serializer reads attributes), never
as ORM instances of another module; ``CashbackPolicy`` instances stay inside ``loyalty`` (its own API in B4b).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID

from django.db.models import (
    BigIntegerField,
    Count,
    DecimalField,
    OuterRef,
    Q,
    QuerySet,
    Subquery,
    Sum,
    Value,
)
from django.db.models.functions import Coalesce

from glowderma.core import enums, timeutils
from glowderma.core.timeutils import TASHKENT
from glowderma.loyalty import engine
from glowderma.loyalty.models import (
    CashbackHold,
    CashbackPolicy,
    CashbackTier,
    LedgerEntry,
    LoyaltyAccount,
    Purchase,
)

# --- Policy versions (design 02 §1.1) ---


def policy_versions() -> list[CashbackPolicy]:
    """Every version, newest first, with ``superseded_by``/``effective_to`` derived, the author joined and the
    tiers prefetched (two queries for ``GET cashback/policy/``)."""
    return list(
        CashbackPolicy.objects.with_derived()
        .select_related("author")
        .prefetch_related("tiers")
        .order_by("-version")
    )


def policy_at(instant: datetime) -> CashbackPolicy | None:
    """The version in force at ``instant`` (newest started, not superseded) with its tiers, or ``None``."""
    policy = CashbackPolicy.objects.active_at(instant)
    if policy is not None:
        policy = CashbackPolicy.objects.prefetch_related("tiers").get(pk=policy.pk)
    return policy


def active_policy(now: datetime | None = None) -> CashbackPolicy | None:
    """The version active now (``policy_at(timeutils.now())``)."""
    return policy_at(now or timeutils.now())


def scheduled_policy(now: datetime | None = None) -> CashbackPolicy | None:
    """The one future, non-superseded version (``state == scheduled``), or ``None``."""
    instant = now or timeutils.now()
    future = (
        CashbackPolicy.objects.with_derived()
        .filter(effective_from__gt=instant)
        .prefetch_related("tiers")
        .order_by("version")
    )
    return next((policy for policy in future if policy.superseded_by is None), None)


def rules_of(policy: CashbackPolicy | None) -> engine.PolicyRules | None:
    """The engine snapshot of ``policy`` (``None`` stays ``None``)."""
    return None if policy is None else engine.rules_for(policy, policy.tiers.all())


def next_effective_from(now: datetime | None = None) -> datetime:
    """Start of the next Tashkent calendar month after ``now`` (the FE's suggested ``effective_from``)."""
    local = (now or timeutils.now()).astimezone(TASHKENT)
    first_of_month = timeutils.month_start(local.date())
    first_of_next = (first_of_month + timedelta(days=32)).replace(day=1)
    return datetime.combine(first_of_next, time.min, tzinfo=TASHKENT)


# --- Customer card numbers (design 02 §4.1–4.3) ---


@dataclass(frozen=True, slots=True)
class Totals:
    """``CustomerCard.totals`` plus the non-reversed online purchase count (contract §2.6)."""

    store: int
    online: int
    lifetime: int
    online_orders_count: int


@dataclass(frozen=True, slots=True)
class AccountSummary:
    balance: int
    held: int
    available: int


@dataclass(frozen=True, slots=True)
class NextTier:
    percent: Decimal
    from_amount: int
    remaining: int
    progress: float


@dataclass(frozen=True, slots=True)
class TierInfo:
    """``CustomerCard.tier``: the band covering the lifetime total and the next one above it."""

    policy_version: int
    percent: Decimal
    next: NextTier | None


def customer_totals(customer_id: int) -> Totals:
    """Σ non-reversed purchase amounts by source (gross, incl. promo and cashback-paid parts)."""
    live = Purchase.objects.filter(customer_id=customer_id, reversed=False)
    row = live.aggregate(
        store=Coalesce(Sum("amount", filter=Q(source=enums.PurchaseSource.STORE_QR)), 0),
        online=Coalesce(Sum("amount", filter=Q(source=enums.PurchaseSource.ONLINE)), 0),
        lifetime=Coalesce(Sum("amount"), 0),
        online_orders_count=Count("id", filter=Q(source=enums.PurchaseSource.ONLINE)),
    )
    return Totals(
        store=int(row["store"]),
        online=int(row["online"]),
        lifetime=int(row["lifetime"]),
        online_orders_count=int(row["online_orders_count"]),
    )


def held_amount(account_id: int) -> int:
    """Σ amount of the account's ``active`` holds (expired-but-unreleased holds still count, L-9)."""
    row = CashbackHold.objects.filter(account_id=account_id, state=enums.CashbackHoldState.ACTIVE).aggregate(
        held=Coalesce(Sum("amount"), 0)
    )
    return int(row["held"])


def account_summary(customer_id: int) -> AccountSummary:
    """``balance``, ``held`` and ``available = balance − held`` in ONE statement (02 §4.2: the account row
    with the active-hold sum as a correlated subquery); no account row → all zero."""
    active_holds = (
        CashbackHold.objects.filter(account_id=OuterRef("pk"), state=enums.CashbackHoldState.ACTIVE)
        .order_by()
        .values("account_id")
        .annotate(total=Sum("amount"))
        .values("total")[:1]
    )
    row = (
        LoyaltyAccount.objects.filter(customer_id=customer_id)
        .annotate(held=Coalesce(Subquery(active_holds, output_field=BigIntegerField()), Value(0)))
        .values_list("balance", "held")
        .first()
    )
    if row is None:
        return AccountSummary(0, 0, 0)
    balance, held = int(row[0]), int(row[1])
    return AccountSummary(balance=balance, held=held, available=balance - held)


def tier_for(lifetime: int, policy: CashbackPolicy | engine.PolicyRules | None = None) -> TierInfo | None:
    """The tier covering ``lifetime`` under ``policy`` (default: the active one); ``None`` if none covers."""
    rules = policy if isinstance(policy, engine.PolicyRules) else rules_of(policy or active_policy())
    if rules is None or rules.version is None:
        return None
    index = engine.tier_index(rules.tiers, lifetime)
    if index is None:
        return None
    current = rules.tiers[index]
    following = rules.tiers[index + 1] if index + 1 < len(rules.tiers) else None
    next_tier = None
    if following is not None:
        span = following.from_amount - current.from_amount
        progress = (lifetime - current.from_amount) / span if span > 0 else 1.0
        next_tier = NextTier(
            percent=following.percent.quantize(engine.PERCENT_QUANTUM),
            from_amount=following.from_amount,
            remaining=following.from_amount - lifetime,
            progress=round(progress, 4),
        )
    return TierInfo(
        policy_version=rules.version, percent=current.percent.quantize(engine.PERCENT_QUANTUM), next=next_tier
    )


# --- Ledger and purchases in the contract shape (design 02 §4.4, contract §2.6) ---


@dataclass(frozen=True, slots=True)
class Actor:
    id: int
    display_name: str


@dataclass(frozen=True, slots=True)
class LedgerRow:
    """Contract §2.6 ``LedgerEntry``; ``source_ref``/``reason`` are ``None`` where the contract says null."""

    id: int
    at: datetime
    kind: str
    amount: int
    balance_after: int
    source_type: str
    source_ref: str | None  # order → order number as string; billz_sale → document no.; admin → None
    order_id: int | None
    base: int | None
    percent: Decimal | None
    promo_excluded: bool
    policy_version: int | None
    reverses_id: int | None
    reason: str | None
    actor: Actor | None


@dataclass(frozen=True, slots=True)
class PurchaseRow:
    """Contract §2.6 ``Purchase``."""

    id: int
    at: datetime
    source: str
    order_id: int | None
    billz_sale_ref: str | None
    amount: int
    promo_amount: int
    cashback: int
    policy_version: int | None
    reversed: bool


def _actor(entry: LedgerEntry) -> Actor | None:
    staff = entry.actor
    if staff is None:
        return None
    return Actor(id=staff.pk, display_name=staff.full_name or staff.email)


def ledger_row(entry: LedgerEntry) -> LedgerRow:
    """One entry (loaded with ``select_related("policy", "actor")``) in the §2.6 shape."""
    return LedgerRow(
        id=entry.pk,
        at=entry.at,
        kind=entry.kind,
        amount=entry.amount,
        balance_after=entry.balance_after,
        source_type=entry.source_type,
        source_ref=entry.source_ref or None,
        order_id=entry.order_id,
        base=entry.base,
        percent=None if entry.percent is None else entry.percent.quantize(engine.PERCENT_QUANTUM),
        promo_excluded=entry.promo_excluded,
        policy_version=None if entry.policy is None else entry.policy.version,
        reverses_id=entry.reverses_id,
        reason=entry.reason or None,
        actor=_actor(entry),
    )


def _ledger_queryset() -> QuerySet[LedgerEntry]:
    return LedgerEntry.objects.select_related("policy", "actor")


def ledger_entry(entry_id: int) -> LedgerRow:
    return ledger_row(_ledger_queryset().get(pk=entry_id))


def ledger_for_customer(customer_id: int) -> list[LedgerRow]:
    """Full history, newest first (``at desc, id desc``); no account → ``[]``."""
    rows = _ledger_queryset().filter(account__customer_id=customer_id).order_by("-at", "-id")
    return [ledger_row(entry) for entry in rows]


def ledger_for_order(order_id: int) -> list[LedgerRow]:
    """``Order.cashback[]``: the order's debit/credit/reversal entries in ``id`` order."""
    rows = _ledger_queryset().filter(order_id=order_id).order_by("id")
    return [ledger_row(entry) for entry in rows]


def purchase_row(purchase: Purchase) -> PurchaseRow:
    return PurchaseRow(
        id=purchase.pk,
        at=purchase.at,
        source=purchase.source,
        order_id=purchase.order_id,
        billz_sale_ref=purchase.billz_sale_ref or None,
        amount=purchase.amount,
        promo_amount=purchase.promo_amount,
        cashback=purchase.cashback_amount,
        policy_version=None if purchase.policy is None else purchase.policy.version,
        reversed=purchase.reversed,
    )


def purchase_for_order(order_id: int) -> PurchaseRow | None:
    """The purchase of a paid order (``Purchase.order`` is 1:1): the id ``reverse_purchase`` needs."""
    purchase = Purchase.objects.select_related("policy").filter(order_id=order_id).first()
    return None if purchase is None else purchase_row(purchase)


@dataclass(frozen=True, slots=True)
class BillzPushInfo:
    """What the ``billz.push_sale`` handler needs (03 §5): the pre-persisted Billz order id and its guard."""

    purchase_id: int
    customer_id: int
    order_id: int | None
    billz_order_uuid: UUID | None
    billz_sale_id: str
    billz_sale_ref: str
    billz_pushed_at: datetime | None  # set → the push already succeeded; the handler skips


def billz_push_info(purchase_id: int) -> BillzPushInfo | None:
    """The Billz-side state of a purchase (``None`` when it does not exist)."""
    purchase = (
        Purchase.objects.filter(pk=purchase_id)
        .only(
            "id",
            "customer_id",
            "order_id",
            "billz_order_uuid",
            "billz_sale_id",
            "billz_sale_ref",
            "billz_pushed_at",
        )
        .first()
    )
    if purchase is None:
        return None
    return BillzPushInfo(
        purchase_id=purchase.pk,
        customer_id=purchase.customer_id,
        order_id=purchase.order_id,
        billz_order_uuid=purchase.billz_order_uuid,
        billz_sale_id=purchase.billz_sale_id,
        billz_sale_ref=purchase.billz_sale_ref,
        billz_pushed_at=purchase.billz_pushed_at,
    )


def purchases_for_customer(customer_id: int) -> list[PurchaseRow]:
    """Full history incl. reversed purchases, newest first (``at desc, id desc``)."""
    rows = Purchase.objects.select_related("policy").filter(customer_id=customer_id).order_by("-at", "-id")
    return [purchase_row(purchase) for purchase in rows]


# --- Segment rules and the customers list (design 02 §4.1, 03 §6.3) ---


def lifetime_totals(customer_ids: list[int] | tuple[int, ...] | set[int]) -> dict[int, int]:
    """``{customer_id: Σ non-reversed purchase amount}`` for every id asked (0 when none)."""
    ids = list(customer_ids)
    totals = dict.fromkeys(ids, 0)
    rows = (
        Purchase.objects.filter(customer_id__in=ids, reversed=False)
        .values("customer_id")
        .annotate(total=Sum("amount"))
        .values_list("customer_id", "total")
    )
    for customer_id, total in rows:
        totals[int(customer_id)] = int(total)
    return totals


def _active_tier_percent(policy: CashbackPolicy | None) -> Any:
    if policy is None:
        return Value(None, output_field=DecimalField(max_digits=5, decimal_places=2))
    tier = CashbackTier.objects.filter(
        Q(to_amount__isnull=True) | Q(to_amount__gt=OuterRef("lifetime_total")),
        policy_id=policy.pk,
        from_amount__lte=OuterRef("lifetime_total"),
    ).order_by("from_amount")
    return Subquery(tier.values("percent")[:1], output_field=DecimalField(max_digits=5, decimal_places=2))


def list_annotations(policy: CashbackPolicy | None = None, *, customer_ref: str = "pk") -> dict[str, Any]:
    """Customer list annotations: ``lifetime_total``, ``balance``, ``percent``, ``online_orders_count``.

    Apply them in this order in **one** ``annotate(**list_annotations())`` call (``percent`` reads the sibling
    ``lifetime_total`` through ``OuterRef``); the list then orders by ``lifetime_total``/``balance`` in SQL,
    ties by ``id``. ``percent`` is the active policy's rate for the lifetime total (``None`` without a policy
    or a covering tier). ``customer_ref`` is the outer column the subqueries correlate on.
    """
    live = Purchase.objects.filter(customer_id=OuterRef(customer_ref), reversed=False).order_by()
    lifetime = live.values("customer_id").annotate(total=Sum("amount")).values("total")[:1]
    online_count = (
        live.filter(source=enums.PurchaseSource.ONLINE)
        .values("customer_id")
        .annotate(n=Count("id"))
        .values("n")[:1]
    )
    balance = LoyaltyAccount.objects.filter(customer_id=OuterRef(customer_ref)).values("balance")[:1]
    return {
        "lifetime_total": Coalesce(Subquery(lifetime, output_field=BigIntegerField()), Value(0)),
        "balance": Coalesce(Subquery(balance, output_field=BigIntegerField()), Value(0)),
        "online_orders_count": Coalesce(Subquery(online_count, output_field=BigIntegerField()), Value(0)),
        "percent": _active_tier_percent(policy or active_policy()),
    }


__all__ = [
    "AccountSummary",
    "Actor",
    "BillzPushInfo",
    "LedgerRow",
    "NextTier",
    "PurchaseRow",
    "TierInfo",
    "Totals",
    "account_summary",
    "active_policy",
    "billz_push_info",
    "customer_totals",
    "held_amount",
    "ledger_entry",
    "ledger_for_customer",
    "ledger_for_order",
    "ledger_row",
    "lifetime_totals",
    "list_annotations",
    "next_effective_from",
    "policy_at",
    "policy_versions",
    "purchase_for_order",
    "purchase_row",
    "purchases_for_customer",
    "rules_of",
    "scheduled_policy",
    "tier_for",
]
