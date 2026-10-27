"""factory-boy factories for the loyalty models (call them as ``XFactory.create(...)``).

``LedgerEntryFactory`` keeps the balance chain consistent the way ``loyalty.services.post_entry`` will: unless
``balance_after`` is passed explicitly it moves ``LoyaltyAccount.balance`` by ``amount`` and stores the new
balance on the row. Traits: ``debit`` (hold capture of an order), ``adjustment`` (admin, with reason/actor),
``store`` (credit of a Billz sale); :func:`reverse_entry` posts the reversal of an entry. ``PurchaseFactory``
builds an online purchase of a paid order (trait ``store`` for a Billz sale); ``credit=<so'm>`` posts the
credit entry and links it in one UPDATE (design 02 §1.6 step 6); :func:`reverse_purchase_rows` flags a
purchase as reversed and reverses its credit (§1.10 at row level). ``CashbackHoldFactory`` has the traits
``captured`` (with its debit entry) and ``released``. ``CashbackPolicyFactory`` takes the next free version
and one default tier ``[0, 20M) 1.00`` (``tiers=[(from, to, "percent"), …]`` overrides, ``tiers=[]`` none).
"""

from __future__ import annotations

import importlib
from collections.abc import Sequence as SequenceType
from datetime import datetime, timedelta
from decimal import Decimal
from hashlib import sha256
from itertools import count
from typing import Any
from uuid import uuid4

from django.db.models import F, Max
from factory.declarations import LazyAttribute, LazyFunction, SelfAttribute, Sequence, SubFactory, Trait
from factory.django import DjangoModelFactory
from factory.helpers import post_generation

from glowderma.accounts.tests.factories import CustomerFactory, StaffUserFactory
from glowderma.core import enums, timeutils
from glowderma.loyalty.models import (
    CashbackHold,
    CashbackPolicy,
    CashbackTier,
    LedgerEntry,
    LoyaltyAccount,
    PosDevice,
    Purchase,
    PurchaseItem,
)

DEFAULT_TIERS: tuple[tuple[int, int | None, str], ...] = ((0, 20_000_000, "1.00"),)
_billz_sale_numbers = count(1)

# ``commerce`` sits above ``loyalty`` and ``catalog`` beside it, yet ``LedgerEntry.order``,
# ``Purchase.order``, ``CashbackHold.order`` and ``PurchaseItem.product`` point at their tables, so the tests
# need real rows of both. factory-boy resolves a dotted ``SubFactory`` target lazily and :func:`upper_factory`
# imports at call time, which keeps the import-linter layers contract (static imports) meaningful for
# application code.
ORDER_FACTORY = "glowderma.commerce.tests.factories.OrderFactory"
PRODUCT_FACTORY = "glowderma.catalog.tests.factories.ProductFactory"


def upper_factory(path: str) -> Any:
    """The factory class at ``path`` (``ORDER_FACTORY`` / ``PRODUCT_FACTORY``), imported at call time."""
    module_name, _, attribute = path.rpartition(".")
    return getattr(importlib.import_module(module_name), attribute)


def _now() -> datetime:
    return timeutils.now()


def _next_version() -> int:
    current = CashbackPolicy.objects.aggregate(top=Max("version"))["top"]
    return 1 if current is None else int(current) + 1


def _next_billz_sale_id() -> str:
    return f"S-{90_000 + next(_billz_sale_numbers)}"


class CashbackPolicyFactory(DjangoModelFactory[CashbackPolicy]):
    class Meta:
        model = CashbackPolicy
        skip_postgeneration_save = True

    version = LazyFunction(_next_version)  # type: ignore[no-untyped-call]
    created_at = LazyFunction(_now)  # type: ignore[no-untyped-call]
    effective_from = LazyAttribute(lambda o: o.created_at + timedelta(days=1))  # type: ignore[no-untyped-call]
    basis = enums.RateBasis.BEFORE_PURCHASE
    earn_on_cashback_paid = False
    author = None

    @post_generation  # type: ignore[untyped-decorator]
    def tiers(
        obj: CashbackPolicy,
        create: bool,
        extracted: SequenceType[tuple[int, int | None, str]] | None,
        **kwargs: Any,
    ) -> None:
        """The default tier (``None``), the given ``(from, to, percent)`` bands, or none (``[]``)."""
        if not create:
            return
        for from_amount, to_amount, percent in DEFAULT_TIERS if extracted is None else extracted:
            CashbackTierFactory.create(
                policy=obj,
                from_amount=from_amount,
                to_amount=to_amount,
                percent=Decimal(percent),
                created_at=obj.created_at,
            )


class CashbackTierFactory(DjangoModelFactory[CashbackTier]):
    class Meta:
        model = CashbackTier

    policy = SubFactory(CashbackPolicyFactory, tiers=[])  # type: ignore[no-untyped-call]
    from_amount = 0
    to_amount = 20_000_000
    percent = Decimal("1.00")
    created_at = LazyAttribute(lambda o: o.policy.created_at)  # type: ignore[no-untyped-call]


class LoyaltyAccountFactory(DjangoModelFactory[LoyaltyAccount]):
    class Meta:
        model = LoyaltyAccount
        django_get_or_create = ("customer",)

    customer = SubFactory(CustomerFactory)  # type: ignore[no-untyped-call]
    balance = 0


class PurchaseFactory(DjangoModelFactory[Purchase]):
    class Meta:
        model = Purchase
        skip_postgeneration_save = True

    class Params:
        store = Trait(  # type: ignore[no-untyped-call]
            source=enums.PurchaseSource.STORE_QR,
            order=None,
            billz_order_uuid=None,
            billz_sale_id=LazyFunction(_next_billz_sale_id),  # type: ignore[no-untyped-call]
            billz_sale_ref=LazyAttribute(lambda o: o.billz_sale_id),  # type: ignore[no-untyped-call]
            amount=231_000,
            promo_amount=0,
            cashback_paid=0,
        )

    customer = SubFactory(CustomerFactory)  # type: ignore[no-untyped-call]
    source = enums.PurchaseSource.ONLINE
    order = SubFactory(ORDER_FACTORY, customer=SelfAttribute("..customer"), paid=True)  # type: ignore[no-untyped-call]
    billz_order_uuid = LazyFunction(uuid4)  # type: ignore[no-untyped-call]
    amount = LazyAttribute(lambda o: 231_000 if o.order is None else o.order.items_subtotal)  # type: ignore[no-untyped-call]
    promo_amount = LazyAttribute(lambda o: 0 if o.order is None else o.order.promo_subtotal)  # type: ignore[no-untyped-call]
    cashback_paid = LazyAttribute(lambda o: 0 if o.order is None else o.order.cashback_used)  # type: ignore[no-untyped-call]
    lifetime_before = 0
    policy = SubFactory(CashbackPolicyFactory)  # type: ignore[no-untyped-call]
    at = LazyAttribute(  # type: ignore[no-untyped-call]
        lambda o: _now() if o.order is None or o.order.paid_at is None else o.order.paid_at
    )

    @post_generation  # type: ignore[untyped-decorator]
    def items(
        obj: Purchase, create: bool, extracted: SequenceType[dict[str, Any]] | None, **kwargs: Any
    ) -> None:
        """Lines summing to ``amount``/``promo_amount`` (``None``), the given lines, or none (``[]``)."""
        if not create:
            return
        if extracted is None:
            regular = obj.amount - obj.promo_amount
            lines = [(regular, False), (obj.promo_amount, True)]
            extracted = [{"unit_price": total, "promo": promo} for total, promo in lines if total > 0]
        for position, line in enumerate(extracted):
            PurchaseItemFactory.create(purchase=obj, position=position, **line)

    @post_generation  # type: ignore[untyped-decorator]
    def credit(obj: Purchase, create: bool, extracted: int | None, **kwargs: Any) -> None:
        """``credit=<so'm>``: post the credit entry, then link it and ``cashback_amount`` in one UPDATE."""
        if not create or not extracted:
            return
        if obj.policy is None:
            raise ValueError("A credited purchase needs a policy (ledger_credit_fields)")
        account, _ = LoyaltyAccount.objects.get_or_create(customer=obj.customer)
        online = obj.source == enums.PurchaseSource.ONLINE
        entry = LedgerEntryFactory.create(
            account=account,
            kind=enums.LedgerKind.CREDIT,
            amount=extracted,
            source_type=enums.LedgerSourceType.ORDER if online else enums.LedgerSourceType.BILLZ_SALE,
            order=obj.order,
            purchase=obj,
            source_ref=str(obj.order_id) if online else obj.billz_sale_ref,
            base=obj.amount - obj.promo_amount,
            percent=kwargs.get("percent", Decimal("1.00")),
            promo_excluded=obj.promo_amount > 0,
            policy=obj.policy,
            at=obj.at,
        )
        Purchase.objects.filter(pk=obj.pk).update(cashback_amount=extracted, credited_entry=entry)
        obj.cashback_amount = extracted
        obj.credited_entry = entry


class PurchaseItemFactory(DjangoModelFactory[PurchaseItem]):
    class Meta:
        model = PurchaseItem

    purchase = SubFactory(PurchaseFactory, items=[])  # type: ignore[no-untyped-call]
    position = LazyAttribute(lambda o: o.purchase.items.count())  # type: ignore[no-untyped-call]
    product = SubFactory(PRODUCT_FACTORY)  # type: ignore[no-untyped-call]
    billz_product_id = LazyAttribute(lambda o: "" if o.product is None else o.product.billz_id)  # type: ignore[no-untyped-call]
    name = LazyAttribute(  # type: ignore[no-untyped-call]
        lambda o: "Billz mahsulot" if o.product is None else (o.product.display_name or o.product.billz_id)
    )
    quantity = 1
    unit_price = LazyAttribute(lambda o: 185_000 if o.product is None else o.product.price)  # type: ignore[no-untyped-call]
    promo = False
    line_total = LazyAttribute(lambda o: o.quantity * o.unit_price)  # type: ignore[no-untyped-call]


class LedgerEntryFactory(DjangoModelFactory[LedgerEntry]):
    """A credit of a paid order by default; ``balance_after`` follows the account unless given explicitly."""

    class Meta:
        model = LedgerEntry

    class Params:
        debit = Trait(  # type: ignore[no-untyped-call]
            kind=enums.LedgerKind.DEBIT,
            amount=-20_000,
            base=None,
            percent=None,
            promo_excluded=False,
            policy=None,
        )
        adjustment = Trait(  # type: ignore[no-untyped-call]
            kind=enums.LedgerKind.ADJUSTMENT,
            source_type=enums.LedgerSourceType.ADMIN,
            order=None,
            purchase=None,
            source_ref="",
            amount=2_950,
            base=None,
            percent=None,
            promo_excluded=False,
            policy=None,
            reason="Kassada QR o‘qilmadi, chek S-87011 qo‘lda bog‘landi",
            actor=SubFactory(StaffUserFactory),  # type: ignore[no-untyped-call]
        )
        store = Trait(  # type: ignore[no-untyped-call]
            source_type=enums.LedgerSourceType.BILLZ_SALE,
            order=None,
            purchase=SubFactory(  # type: ignore[no-untyped-call]
                PurchaseFactory,
                store=True,
                customer=SelfAttribute("..account.customer"),  # type: ignore[no-untyped-call]
            ),
            source_ref=LazyAttribute(lambda o: o.purchase.billz_sale_ref),  # type: ignore[no-untyped-call]
            amount=2_310,
            base=231_000,
        )

    account = SubFactory(LoyaltyAccountFactory)  # type: ignore[no-untyped-call]
    kind = enums.LedgerKind.CREDIT
    amount = 1_850
    source_type = enums.LedgerSourceType.ORDER
    order = SubFactory(ORDER_FACTORY, customer=SelfAttribute("..account.customer"), paid=True)  # type: ignore[no-untyped-call]
    purchase = None
    source_ref = LazyAttribute(lambda o: "" if o.order is None else str(o.order.pk))  # type: ignore[no-untyped-call]
    base = 185_000
    percent = Decimal("1.00")
    promo_excluded = False
    policy = SubFactory(CashbackPolicyFactory)  # type: ignore[no-untyped-call]
    reverses = None
    reason = ""
    actor = None
    at = LazyFunction(_now)  # type: ignore[no-untyped-call]

    @classmethod
    def _create(cls, model_class: type[LedgerEntry], *args: Any, **kwargs: Any) -> LedgerEntry:
        if "balance_after" not in kwargs:
            account: LoyaltyAccount = kwargs["account"]
            LoyaltyAccount.objects.filter(pk=account.pk).update(balance=F("balance") + kwargs["amount"])
            account.refresh_from_db(fields=["balance"])
            kwargs["balance_after"] = account.balance
        entry: LedgerEntry = super()._create(model_class, *args, **kwargs)  # type: ignore[no-untyped-call]
        return entry


def reverse_entry(entry: LedgerEntry, *, at: datetime | None = None) -> LedgerEntry:
    """The reversal of ``entry`` as ``reverse_order_entries`` posts it (same source, negated amount)."""
    return LedgerEntryFactory.create(
        account=entry.account,
        kind=enums.LedgerKind.REVERSAL,
        amount=-entry.amount,
        source_type=entry.source_type,
        order=entry.order,
        purchase=entry.purchase,
        source_ref=entry.source_ref,
        base=None,
        percent=None,
        promo_excluded=False,
        policy=None,
        reverses=entry,
        at=at or _now(),
    )


def reverse_purchase_rows(purchase: Purchase, *, at: datetime | None = None) -> LedgerEntry | None:
    """Flag ``purchase`` reversed and reverse its credit (design 02 §1.10, rows only); return the reversal."""
    reversed_at = at or _now()
    reversal = (
        None if purchase.credited_entry is None else reverse_entry(purchase.credited_entry, at=reversed_at)
    )
    Purchase.objects.filter(pk=purchase.pk).update(
        reversed=True, reversed_at=reversed_at, reversal_entry=reversal, updated_at=reversed_at
    )
    purchase.refresh_from_db()
    return reversal


class CashbackHoldFactory(DjangoModelFactory[CashbackHold]):
    """An active hold on an unpaid order; ``captured=True`` posts its debit, ``released=True`` expires it."""

    class Meta:
        model = CashbackHold

    class Params:
        captured = Trait(  # type: ignore[no-untyped-call]
            state=enums.CashbackHoldState.CAPTURED,
            order__paid=True,
            captured_at=LazyAttribute(lambda o: o.order.paid_at or _now()),  # type: ignore[no-untyped-call]
            captured_entry=SubFactory(  # type: ignore[no-untyped-call]
                LedgerEntryFactory,
                debit=True,
                account=SelfAttribute("..account"),  # type: ignore[no-untyped-call]
                order=SelfAttribute("..order"),  # type: ignore[no-untyped-call]
                amount=LazyAttribute(lambda e: -e.factory_parent.amount),  # type: ignore[no-untyped-call]
                at=SelfAttribute("..captured_at"),  # type: ignore[no-untyped-call]
            ),
        )
        released = Trait(  # type: ignore[no-untyped-call]
            state=enums.CashbackHoldState.RELEASED,
            released_at=LazyAttribute(lambda o: o.expires_at),  # type: ignore[no-untyped-call]
            release_reason=CashbackHold.ReleaseReason.RESERVATION_EXPIRED,
        )

    account = SubFactory(LoyaltyAccountFactory, balance=100_000)  # type: ignore[no-untyped-call]
    order = SubFactory(  # type: ignore[no-untyped-call]
        ORDER_FACTORY,
        customer=SelfAttribute("..account.customer"),  # type: ignore[no-untyped-call]
        cashback_used=20_000,
    )
    amount = LazyAttribute(lambda o: o.order.cashback_used)  # type: ignore[no-untyped-call]
    state = enums.CashbackHoldState.ACTIVE
    expires_at = LazyAttribute(  # type: ignore[no-untyped-call]
        lambda o: o.order.reservation_expires_at or (o.order.created_at + timedelta(minutes=15))
    )


class PosDeviceFactory(DjangoModelFactory[PosDevice]):
    class Meta:
        model = PosDevice

    name = Sequence(lambda n: f"Kassa {n + 1}")  # type: ignore[no-untyped-call]
    location_label = "Chilonzor do‘koni"
    key_prefix = Sequence(lambda n: f"gdpos-{n + 1:04d}"[:12])  # type: ignore[no-untyped-call]
    key_hash = Sequence(lambda n: sha256(f"gdpos-{n + 1:04d}-secret".encode()).hexdigest())  # type: ignore[no-untyped-call]
    is_active = True
    created_by = None


__all__ = [
    "DEFAULT_TIERS",
    "ORDER_FACTORY",
    "PRODUCT_FACTORY",
    "CashbackHoldFactory",
    "CashbackPolicyFactory",
    "CashbackTierFactory",
    "LedgerEntryFactory",
    "LoyaltyAccountFactory",
    "PosDeviceFactory",
    "PurchaseFactory",
    "PurchaseItemFactory",
    "reverse_entry",
    "reverse_purchase_rows",
    "upper_factory",
]
