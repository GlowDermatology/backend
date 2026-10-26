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

from django.conf import settings
from django.contrib.postgres.expressions import ArraySubquery
from django.db.models import (
    BigIntegerField,
    Case,
    Count,
    DecimalField,
    OuterRef,
    Q,
    QuerySet,
    Subquery,
    Sum,
    Value,
    When,
)
from django.db.models.functions import Coalesce

from glowderma.core import enums, i18n, timeutils
from glowderma.core.timeutils import TASHKENT
from glowderma.loyalty import engine, hold_grace
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
class HoldView:
    """One order's cashback hold (contract §2.2 ``cashback_hold`` keys plus the id)."""

    id: int
    amount: int
    state: str
    expires_at: datetime


def hold_for_order(order_id: int) -> HoldView | None:
    """The order's hold: the ``active`` one (at most one, a DB constraint), else the newest; ``None``
    without. ``Order.cashback_hold`` renders only an ``active`` hold (contract §7 item 18): the caller checks
    ``state``.
    """
    active_first = Case(When(state=enums.CashbackHoldState.ACTIVE, then=Value(0)), default=Value(1))
    hold = CashbackHold.objects.filter(order_id=order_id).order_by(active_first, "-id").first()  # one query
    if hold is None:
        return None
    return HoldView(id=hold.pk, amount=hold.amount, state=hold.state, expires_at=hold.expires_at)


def store_sale_ids(customer_id: int) -> set[str]:
    """The Billz sale ids of the customer's recorded store purchases (the demo seeder skips those)."""
    return set(
        Purchase.objects.filter(customer_id=customer_id, source=enums.PurchaseSource.STORE_QR).values_list(
            "billz_sale_id", flat=True
        )
    )


def has_adjustment(customer_id: int, reason: str) -> bool:
    """Whether the customer's ledger holds an ``adjustment`` with exactly this reason (demo seeder check)."""
    return LedgerEntry.objects.filter(
        account__customer_id=customer_id, kind=enums.LedgerKind.ADJUSTMENT, reason=reason
    ).exists()


def has_ledger_entries() -> bool:
    """Whether any ledger entry exists (the commerce demo seeder's preflight)."""
    return LedgerEntry.objects.exists()


def ledger_table() -> str:
    """The ledger's table name: the demo seeders pin and park its id sequence (``core.seeding``)."""
    return LedgerEntry._meta.db_table


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
    billz_payment_attempted_at: datetime | None = None  # a Billz payment whose outcome is not known yet


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
            "billz_payment_attempted_at",
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
        billz_payment_attempted_at=purchase.billz_payment_attempted_at,
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


# --- Customer API (customer contract §2.9, plan §4 P3) ---


@dataclass(frozen=True, slots=True)
class EffectiveHold:
    """One active hold as the customer sees it (``GET me/cashback/`` ``holds[]``)."""

    order_id: int
    amount: int
    expires_at: datetime  # the EFFECTIVE expiry (C§2.9.1)


def effective_holds(customer_id: int, now: datetime | None = None) -> list[EffectiveHold]:
    """The customer's ``active`` holds with their effective expiry, oldest first.

    ``CashbackHold.expires_at`` stays at the order's ``reservation_expires_at``. An active hold whose
    ``expires_at`` has passed is shown with ``expires_at + PAYMENT_IN_FLIGHT_GRACE_SECONDS`` while its order
    has an open transaction attached in time (``hold_grace``, answered by commerce: layering), otherwise
    with its own ``expires_at`` until the expiry job releases it; any other with ``expires_at``.
    """
    rows = (
        CashbackHold.objects.filter(account__customer_id=customer_id, state=enums.CashbackHoldState.ACTIVE)
        .order_by("expires_at", "id")
        .values_list("order_id", "amount", "expires_at")
    )
    return _effective(rows, timeutils.now() if now is None else now)


def _effective(rows: Any, instant: datetime) -> list[EffectiveHold]:
    rows = list(rows)
    grace = timedelta(seconds=int(settings.PAYMENT_IN_FLIGHT_GRACE_SECONDS))
    overdue = {int(order_id) for order_id, _, expires_at in rows if expires_at <= instant}
    kept = hold_grace.orders_in_grace(overdue, instant) if overdue else set()
    return [
        EffectiveHold(
            order_id=int(order_id),
            amount=int(amount),
            expires_at=expires_at + grace if int(order_id) in kept else expires_at,
        )
        for order_id, amount, expires_at in rows
    ]


def _account_with_holds(customer_id: int) -> tuple[int, list[tuple[int, int, datetime]]]:
    """The balance and the active holds ``(order_id, amount, expires_at)`` (oldest first) in ONE statement,
    so ``held``/``available`` and ``holds[]`` of one response come from the same snapshot (a hold captured
    or released between two READ COMMITTED reads would otherwise make them disagree). No account → 0, []."""
    active = CashbackHold.objects.filter(
        account_id=OuterRef("pk"), state=enums.CashbackHoldState.ACTIVE
    ).order_by("expires_at", "id")
    row = (
        LoyaltyAccount.objects.filter(customer_id=customer_id)
        .annotate(
            hold_orders=ArraySubquery(active.values("order_id")),
            hold_amounts=ArraySubquery(active.values("amount")),
            hold_expiries=ArraySubquery(active.values("expires_at")),
        )
        .values_list("balance", "hold_orders", "hold_amounts", "hold_expiries")
        .first()
    )
    if row is None:
        return 0, []
    balance, orders, amounts, expiries = row
    return int(balance), list(zip(orders, amounts, expiries, strict=True))


@dataclass(frozen=True, slots=True)
class TierBandView:
    percent: Decimal
    from_amount: int
    to_amount: int | None


@dataclass(frozen=True, slots=True)
class CashbackSummary:
    """C§2.9.1 ``GET me/cashback/`` (``qr_ttl_seconds`` is added by the view from the settings)."""

    balance: int
    held: int
    available: int
    negative: bool
    lifetime_total: int
    tier: TierBandView | None
    next_tier: NextTier | None
    tiers: list[TierBandView]
    earn_on_cashback_paid: bool
    holds: list[EffectiveHold]


def cashback_summary(customer_id: int, now: datetime | None = None) -> CashbackSummary:
    """The customer's cashback screen: the same numbers as the admin card (``account_summary``'s balance and
    active-hold sum, ``customer_totals``, ``tier_for`` under the active policy) plus the policy bands and the
    effective holds; balance, ``held`` and ``holds[]`` are read in one statement."""
    instant = timeutils.now() if now is None else now
    balance, hold_rows = _account_with_holds(customer_id)
    held = sum(amount for _order, amount, _expires in hold_rows)
    lifetime = customer_totals(customer_id).lifetime
    rules = rules_of(active_policy(instant))
    tiers: list[TierBandView] = []
    band: TierBandView | None = None
    next_tier: NextTier | None = None
    if rules is not None:
        tiers = [
            TierBandView(tier.percent.quantize(engine.PERCENT_QUANTUM), tier.from_amount, tier.to_amount)
            for tier in rules.tiers
        ]
        index = engine.tier_index(rules.tiers, lifetime)
        if index is not None:
            band = tiers[index]
            info = tier_for(lifetime, rules)
            next_tier = info.next if info is not None else None
    return CashbackSummary(
        balance=balance,
        held=held,
        available=balance - held,
        negative=balance < 0,
        lifetime_total=lifetime,
        tier=band,
        next_tier=next_tier,
        tiers=tiers,
        earn_on_cashback_paid=bool(rules.earn_on_cashback_paid) if rules is not None else False,
        holds=_effective(hold_rows, instant),
    )


def customer_ledger_page(customer_id: int) -> QuerySet[LedgerEntry]:
    """The customer's own entries, newest first (``at desc, id desc``), as a queryset the view paginates in
    SQL (``ledger_for_customer`` builds a full Python list and is not used for pages)."""
    return _ledger_queryset().filter(account__customer_id=customer_id).order_by("-at", "-id")


LEDGER_SOURCES = {
    enums.LedgerSourceType.ORDER: enums.LedgerSource.ONLINE,
    enums.LedgerSourceType.BILLZ_SALE: enums.LedgerSource.STORE,
    enums.LedgerSourceType.ADMIN: enums.LedgerSource.ADMIN,
}


@dataclass(frozen=True, slots=True)
class LedgerItemView:
    """C§2.9.2 ``LedgerItem``: rendered in the request language; the staff ``reason`` never appears."""

    id: int
    at: datetime
    kind: str
    amount: int
    balance_after: int
    title: str
    subtitle: str | None
    order_id: int | None
    source: str


def _ledger_texts(entry: LedgerEntry, lang: str) -> tuple[str, str | None]:
    """``(title, subtitle)`` of one entry by kind and source (C§2.9.2 table)."""
    store = entry.source_type == enums.LedgerSourceType.BILLZ_SALE
    order_id = entry.order_id
    if entry.kind == enums.LedgerKind.CREDIT:
        percent = i18n.format_percent(entry.percent if entry.percent is not None else 0, lang)
        if store:
            return i18n.t("label.ledger.credit", lang), i18n.t(
                "label.ledger.sub_store_percent", lang, percent=percent
            )
        return i18n.t("label.ledger.credit", lang), i18n.t(
            "label.ledger.sub_order_percent", lang, order_id=order_id, percent=percent
        )
    if entry.kind == enums.LedgerKind.DEBIT:
        return i18n.t("label.ledger.debit", lang), i18n.t("label.ledger.sub_order", lang, order_id=order_id)
    if entry.kind == enums.LedgerKind.REVERSAL:
        title_key = "label.ledger.reversal_back" if entry.amount > 0 else "label.ledger.reversal_taken"
        if store:
            return i18n.t(title_key, lang), i18n.t("label.ledger.sub_store_returned", lang)
        return i18n.t(title_key, lang), i18n.t("label.ledger.sub_order_cancelled", lang, order_id=order_id)
    # adjustment: generic texts; the staff reason is internal (M2-N1)
    title_key = "label.ledger.adjust_add" if entry.amount > 0 else "label.ledger.adjust_subtract"
    return i18n.t(title_key, lang), None


def ledger_item_view(entry: LedgerEntry, lang: str, horizon: datetime | None) -> LedgerItemView:
    """One entry in the C§2.9.2 shape. Before the history ``horizon`` (``Customer.history_from``) the entry
    keeps ``amount``/``balance_after`` but loses its order link and reads "Avvalgi hisob"."""
    title, subtitle = _ledger_texts(entry, lang)
    order_id = entry.order_id
    if horizon is not None and entry.at < horizon:
        order_id = None
        subtitle = i18n.t("label.ledger.sub_previous", lang)
    return LedgerItemView(
        id=entry.pk,
        at=entry.at,
        kind=entry.kind,
        amount=entry.amount,
        balance_after=entry.balance_after,
        title=title,
        subtitle=subtitle,
        order_id=order_id,
        source=LEDGER_SOURCES[enums.LedgerSourceType(entry.source_type)],
    )


__all__ = [
    "AccountSummary",
    "Actor",
    "EffectiveHold",
    "LedgerItemView",
    "BillzPushInfo",
    "CashbackSummary",
    "TierBandView",
    "LedgerRow",
    "NextTier",
    "PurchaseRow",
    "TierInfo",
    "Totals",
    "account_summary",
    "active_policy",
    "billz_push_info",
    "cashback_summary",
    "customer_ledger_page",
    "customer_totals",
    "effective_holds",
    "held_amount",
    "ledger_entry",
    "ledger_for_customer",
    "ledger_for_order",
    "ledger_item_view",
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
