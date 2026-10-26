"""Transactional writes of `loyalty` (design 02 §1.3–§1.6, §1.9.2, §1.10; contract §2.6 / §2.7).

Scalar in, values out: callers pass ids, amounts and instants (never a ``commerce.Order`` instance) and get
frozen dataclasses or ids back, so ``commerce``/``customers`` never hold ``loyalty`` ORM rows.

Locking (02 §1.9.2): the global order is Order → Payment → LoyaltyAccount. Every writer here locks **only**
the ``LoyaltyAccount`` row (``select_for_update(of=("self",))``) and never touches a commerce or payments
row, so a caller that already holds an Order (and Payment) lock may call any service inside its transaction;
PostgreSQL treats the re-acquired account lock as already held. :func:`post_entry` is the **only** place
``LoyaltyAccount.balance`` changes: an ``F()`` update under the row lock, the new balance stored as
``balance_after`` on the entry, in one transaction. Negative balances are allowed (L5) and flagged with the
audit action ``loyalty.balance_negative`` when a posting takes the balance below zero.

Posting order: under the account lock ids grow in insert order, and every entry's ``at`` is at least the
``at`` of the account's newest entry. An explicit ``at`` earlier than that raises :class:`OutOfOrderPosting`
(the ``balance_after`` chain read newest-first by ``at`` would otherwise not be a running sum); the default
instant is read **after** the lock and never goes backwards. Every datetime argument must be timezone-aware
(``ValueError`` otherwise).

Typed exceptions: :class:`InsufficientBalance`, :class:`AdjustmentValidationError` and
:class:`PolicyValidationError` carry contract field errors (``core.api.exceptions.field_error``) and the
top-level message of the contract table, ready for B4b/B5 to turn into HTTP 400; hold state mismatches raise
``core.api.exceptions.InvalidTransition`` (409); an unknown purchase raises :class:`PurchaseNotFound`; a
replay that names another customer, or a sale/order that belongs to another purchase, raises
:class:`PurchaseConflict` (:class:`BillzSaleConflict` for Billz sale ids).
"""

from __future__ import annotations

import hashlib
import logging
import secrets
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

from django.db import IntegrityError, connection, transaction
from django.db.models import F, Max
from django.utils import timezone
from rest_framework.exceptions import ErrorDetail

from glowderma.accounts.models import StaffUser
from glowderma.core import audit, enums, timeutils
from glowderma.core.api.exceptions import InvalidTransition, field_error
from glowderma.loyalty import engine, selectors
from glowderma.loyalty.engine import TierBand
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
from glowderma.loyalty.selectors import active_policy, next_effective_from, policy_at, scheduled_policy

logger = logging.getLogger(__name__)

# Contract §2.6 adjustment rules
ADJUSTMENT_MIN_AMOUNT = 1
ADJUSTMENT_MAX_AMOUNT = 10_000_000
REASON_MIN_LENGTH = 3
REASON_MAX_LENGTH = 300
MESSAGE_AMOUNT = "Summani kiriting."
MESSAGE_REASON = "Sababni kiriting."
MESSAGE_INSUFFICIENT = "Balans manfiy bo‘lib qoladi."
MESSAGE_INSUFFICIENT_FIELD = "Balansdan ko‘p ayirib bo‘lmaydi."
# Contract §2.7 policy rules
MESSAGE_MIN_ITEMS = "Kamida bitta daraja kerak."
MESSAGE_EMPTY_RANGE = "Yuqori chegara quyi chegaradan katta bo‘lsin."
MESSAGE_OVERLAP = "Darajalar ustma-ust tushmoqda."
MESSAGE_OUT_OF_RANGE = "Foiz 0 dan katta va 100 dan oshmasligi kerak."
MESSAGE_FIELDS = "Maydonlarni tekshiring."
MESSAGE_IN_PAST = "Kelajakdagi sanani tanlang."
POLICY_ADVISORY_LOCK = "loyalty.cashbackpolicy"
BILLZ_SALE_ID_MAX_LENGTH = 64  # Purchase.billz_sale_id
BILLZ_SALE_REF_MAX_LENGTH = 32  # Purchase.billz_sale_ref (also the store credit's source_ref)
AUDIT_POLICY_ENTITY = "cashback_policy"


# --- Exceptions ---


class LoyaltyError(Exception):
    """Base class of the module's typed errors."""


class LoyaltyValidationError(LoyaltyError):
    """Contract field errors (``{field: [ErrorDetail]}``) plus the table's top-level ``message``."""

    def __init__(self, errors: dict[str, list[ErrorDetail]], message: str) -> None:
        super().__init__(message)
        self.errors = errors
        self.message = message


class InsufficientBalance(LoyaltyValidationError):
    """``amount > available`` under the account lock (subtract adjustment, hold creation)."""

    def __init__(self, *, available: int, requested: int, field: str = "amount") -> None:
        super().__init__(
            {field: [field_error("insufficient_balance", MESSAGE_INSUFFICIENT_FIELD)]}, MESSAGE_INSUFFICIENT
        )
        self.available = available
        self.requested = requested


class AdjustmentValidationError(LoyaltyValidationError):
    """The ``amount``/``reason`` rules of ``POST customers/{id}/cashback-adjustments/``."""


class PolicyValidationError(LoyaltyValidationError):
    """The tier / ``effective_from`` rules of ``POST cashback/policy-versions/``."""


class PurchaseNotFound(LoyaltyError):
    pass


class OutOfOrderPosting(LoyaltyError, ValueError):
    """An explicit ``at`` earlier than the account's newest entry (the chain must follow posting order)."""

    def __init__(self, *, account_id: int, at: datetime, latest_at: datetime) -> None:
        super().__init__(
            f"Ledger entry of account {account_id} at {at.isoformat()} is earlier than its newest entry "
            f"({latest_at.isoformat()})"
        )
        self.account_id = account_id
        self.at = at
        self.latest_at = latest_at


class PurchaseConflict(LoyaltyError):
    """The order (or Billz order id) already belongs to another customer's purchase."""


class BillzSaleConflict(PurchaseConflict):
    """The Billz sale belongs to another customer or purchase, or the purchase is not an online one."""


# --- Results ---


@dataclass(frozen=True, slots=True)
class PostedEntry:
    """A ledger entry as :func:`post_entry` wrote it (what ``cashback_credited`` events need)."""

    entry_id: int
    account_id: int
    kind: str
    amount: int
    balance_after: int
    at: datetime
    base: int | None = None
    percent: Decimal | None = None
    policy_version: int | None = None
    reverses_id: int | None = None


@dataclass(frozen=True, slots=True)
class HoldResult:
    hold_id: int
    account_id: int
    order_id: int
    amount: int
    state: str
    expires_at: datetime
    captured_entry_id: int | None
    changed: bool  # False = idempotent no-op (already in the target state)


@dataclass(frozen=True, slots=True)
class PurchaseLine:
    """One line snapshot the orchestrator copies from ``OrderItem`` (or a Billz receipt line)."""

    name: str
    quantity: int
    unit_price: int
    line_total: int
    promo: bool = False
    product_id: int | None = None
    billz_product_id: str = ""


@dataclass(frozen=True, slots=True)
class PurchaseResult:
    purchase_id: int
    customer_id: int
    source: str
    order_id: int | None
    billz_order_uuid: UUID | None
    billz_sale_id: str
    billz_sale_ref: str
    amount: int
    promo_amount: int
    cashback_paid: int
    lifetime_before: int
    policy_version: int | None
    base: int
    percent: Decimal | None
    promo_excluded: bool
    cashback_amount: int
    credited_entry_id: int | None
    at: datetime
    created: bool  # False = the purchase existed already (idempotent replay)


@dataclass(frozen=True, slots=True)
class ReversalResult:
    purchase_id: int
    reversed_at: datetime
    reversal_entry_ids: tuple[int, ...]  # newly posted this call, in posting order
    credit_reversal_entry_id: int | None  # the reversal of ``credited_entry`` (stored as reversal_entry)
    net_amount: int  # Σ of the new reversal amounts (+cashback_used − cashback_amount for a paid order)
    already_reversed: bool


@dataclass(frozen=True, slots=True)
class LinkResult:
    purchase_id: int
    billz_order_uuid: UUID | None
    billz_sale_id: str
    billz_sale_ref: str
    billz_pushed_at: datetime | None
    changed: bool


@dataclass(frozen=True, slots=True)
class AdjustmentResult:
    entry_id: int
    customer_id: int
    account_id: int
    balance: int
    held: int
    available: int


@dataclass(frozen=True, slots=True)
class TierWarning:
    """Contract §2.7 ``warnings[]``: ``gap`` ``[from, to)`` uncovered, ``uncovered_above`` (``to`` null)."""

    code: str
    from_amount: int
    to_amount: int | None


@dataclass(frozen=True, slots=True)
class PolicyVersionResult:
    version: int
    policy_id: int
    superseded_version: int | None
    warnings: tuple[TierWarning, ...]


# --- Helpers ---


def _aware(value: datetime | None, name: str) -> None:
    """Refuse naive datetimes before any DB work (Django would store them as Tashkent wall time)."""
    if value is not None and timezone.is_naive(value):
        raise ValueError(f"{name} must be timezone-aware")


def _check_billz_refs(billz_sale_id: str, billz_sale_ref: str) -> None:
    if len(billz_sale_id) > BILLZ_SALE_ID_MAX_LENGTH:
        raise ValueError(f"billz_sale_id is longer than {BILLZ_SALE_ID_MAX_LENGTH} characters")
    if len(billz_sale_ref) > BILLZ_SALE_REF_MAX_LENGTH:
        raise ValueError(
            f"billz_sale_ref is longer than {BILLZ_SALE_REF_MAX_LENGTH} characters "
            "(pass an explicit document number when the sale id is longer)"
        )


def _staff(staff_id: int | None) -> StaffUser | None:
    return None if staff_id is None else StaffUser.objects.get(pk=staff_id)


def _lock_account(customer_id: int) -> LoyaltyAccount:
    """The customer's account row, created lazily, locked ``FOR UPDATE OF`` itself (never the customer)."""
    account, _ = LoyaltyAccount.objects.select_for_update(of=("self",)).get_or_create(customer_id=customer_id)
    return account


def _lock_account_by_id(account_id: int) -> LoyaltyAccount:
    return LoyaltyAccount.objects.select_for_update(of=("self",)).get(pk=account_id)


def _policy_id(policy_version: int | None) -> int | None:
    if policy_version is None:
        return None
    policy_id = CashbackPolicy.objects.filter(version=policy_version).values_list("id", flat=True).first()
    if policy_id is None:
        raise LoyaltyError(f"Unknown cashback policy version {policy_version}")
    return int(policy_id)


def _hold_result(hold: CashbackHold, *, changed: bool) -> HoldResult:
    return HoldResult(
        hold_id=hold.pk,
        account_id=hold.account_id,
        order_id=hold.order_id,
        amount=hold.amount,
        state=hold.state,
        expires_at=hold.expires_at,
        captured_entry_id=hold.captured_entry_id,
        changed=changed,
    )


def _purchase_result(purchase: Purchase, *, created: bool) -> PurchaseResult:
    """The result of a stored purchase: the engine re-run on its snapshotted inputs and (immutable) policy,
    so an idempotent replay equals the first call except for ``created``."""
    result = engine.credit_for_purchase(
        amount=purchase.amount,
        promo_amount=purchase.promo_amount,
        cashback_paid=purchase.cashback_paid,
        lifetime_before=purchase.lifetime_before,
        policy=selectors.rules_of(purchase.policy),
    )
    return PurchaseResult(
        purchase_id=purchase.pk,
        customer_id=purchase.customer_id,
        source=purchase.source,
        order_id=purchase.order_id,
        billz_order_uuid=purchase.billz_order_uuid,
        billz_sale_id=purchase.billz_sale_id,
        billz_sale_ref=purchase.billz_sale_ref,
        amount=purchase.amount,
        promo_amount=purchase.promo_amount,
        cashback_paid=purchase.cashback_paid,
        lifetime_before=purchase.lifetime_before,
        policy_version=None if purchase.policy is None else purchase.policy.version,
        base=result.eligible,
        percent=result.percent,
        promo_excluded=result.promo_excluded,
        cashback_amount=purchase.cashback_amount,
        credited_entry_id=purchase.credited_entry_id,
        at=purchase.at,
        created=created,
    )


def _load_purchase(**lookup: Any) -> Purchase | None:
    return (
        Purchase.objects.select_related("policy").prefetch_related("policy__tiers").filter(**lookup).first()
    )


# --- Accounts and ledger ---


def get_or_create_account(customer_id: int) -> int:
    """The id of the customer's ``LoyaltyAccount`` (created with balance 0 on first use)."""
    account, _ = LoyaltyAccount.objects.get_or_create(customer_id=customer_id)
    return account.pk


def lock_account(customer_id: int) -> datetime | None:
    """Lock the customer's account row ``FOR UPDATE OF`` (created lazily) without writing; return the newest
    ledger entry's ``at`` (``None`` for an empty ledger).

    For an orchestrating flow that must pick its posting instant under the lock (``post_entry`` refuses an
    ``at`` older than the newest entry, T19). The caller owns the transaction, which keeps the lock until it
    commits; lock order Order → Payment → LoyaltyAccount still applies.
    """
    account = _lock_account(customer_id)
    return (
        LedgerEntry.objects.filter(account_id=account.pk).order_by("-at").values_list("at", flat=True).first()
    )


def post_entry(
    account_id: int,
    *,
    kind: str,
    amount: int,
    source_type: str,
    order_id: int | None = None,
    purchase_id: int | None = None,
    source_ref: str = "",
    base: int | None = None,
    percent: Decimal | None = None,
    promo_excluded: bool = False,
    policy_version: int | None = None,
    reverses_id: int | None = None,
    reason: str = "",
    actor_id: int | None = None,
    at: datetime | None = None,
) -> PostedEntry:
    """Append one signed ledger entry and move the balance by ``amount`` — THE only balance writer.

    Runs in (or joins) a transaction, locks the account row, applies ``F("balance") + amount``, re-reads the
    balance under the same lock and stores it as ``balance_after``. ``amount == 0`` is refused (callers skip
    zero credits). ``at`` is the business time (aware; default ``timeutils.now()`` read under the lock); an
    ``at`` earlier than the account's newest entry raises :class:`OutOfOrderPosting`. ``actor_id``/``reason``
    are stored on adjustments only (the DB rejects them elsewhere). A balance that drops below zero is
    audited as ``loyalty.balance_negative`` (L5).
    """
    _aware(at, "at")
    actor = _staff(actor_id)
    return _post_entry(
        account_id,
        kind=kind,
        amount=amount,
        source_type=source_type,
        order_id=order_id,
        purchase_id=purchase_id,
        source_ref=source_ref,
        base=base,
        percent=percent,
        promo_excluded=promo_excluded,
        policy_id=_policy_id(policy_version),
        reverses_id=reverses_id,
        reason=reason,
        actor=actor,
        at=at,
        audit_actor=actor,
    )


def _post_entry(
    account_id: int,
    *,
    kind: str,
    amount: int,
    source_type: str,
    order_id: int | None = None,
    purchase_id: int | None = None,
    source_ref: str = "",
    base: int | None = None,
    percent: Decimal | None = None,
    promo_excluded: bool = False,
    policy_id: int | None = None,
    reverses_id: int | None = None,
    reason: str = "",
    actor: StaffUser | None = None,
    at: datetime | None = None,
    audit_actor: StaffUser | None = None,
) -> PostedEntry:
    if amount == 0:
        raise ValueError("A ledger entry never moves the balance by zero")
    _aware(at, "at")
    with transaction.atomic():
        account = _lock_account_by_id(account_id)
        # under the lock: the newest entry is final, and this entry gets the next id
        latest_at = (
            LedgerEntry.objects.filter(account_id=account.pk)
            .order_by("-at")
            .values_list("at", flat=True)
            .first()
        )
        if at is None:
            instant = timeutils.now() if latest_at is None else max(timeutils.now(), latest_at)
        elif latest_at is not None and at < latest_at:
            raise OutOfOrderPosting(account_id=account.pk, at=at, latest_at=latest_at)
        else:
            instant = at
        balance_before = account.balance
        LoyaltyAccount.objects.filter(pk=account.pk).update(
            balance=F("balance") + amount, updated_at=timeutils.now()
        )
        account.refresh_from_db(fields=["balance"])
        entry = LedgerEntry.objects.create(
            account=account,
            kind=kind,
            amount=amount,
            balance_after=account.balance,
            source_type=source_type,
            order_id=order_id,
            purchase_id=purchase_id,
            source_ref=source_ref,
            base=base,
            percent=None if percent is None else percent.quantize(engine.PERCENT_QUANTUM),
            promo_excluded=promo_excluded,
            policy_id=policy_id,
            reverses_id=reverses_id,
            reason=reason,
            actor=actor,
            at=instant,
        )
        if balance_before >= 0 > account.balance:
            audit.record(
                action="loyalty.balance_negative",
                entity_type="loyalty_account",
                entity_id=str(account.pk),
                actor=audit_actor,
                before={"balance": balance_before},
                after={
                    "customer_id": account.customer_id,
                    "balance": account.balance,
                    "entry_id": entry.pk,
                    "kind": kind,
                    "amount": amount,
                },
            )
    policy_version = None
    if policy_id is not None:
        policy_version = int(
            CashbackPolicy.objects.filter(pk=policy_id).values_list("version", flat=True).get()
        )
    return PostedEntry(
        entry_id=entry.pk,
        account_id=account.pk,
        kind=kind,
        amount=amount,
        balance_after=account.balance,
        at=instant,
        base=base,
        percent=entry.percent,
        policy_version=policy_version,
        reverses_id=reverses_id,
    )


# --- Holds (design 02 §1.5) ---


def create_hold(customer_id: int, order_id: int, amount: int, expires_at: datetime) -> HoldResult:
    """Reserve ``amount`` of the customer's available cashback for an unpaid order.

    ``available = balance − Σ active holds`` is checked under the account lock (``InsufficientBalance``). One
    hold per order (02 §1.5): an existing ``active`` or ``captured`` hold of the order is returned unchanged
    (``changed=False``, so an at-least-once checkout replay never reserves or spends twice); a ``released``
    one raises ``InvalidTransition`` (the order was cancelled with it and is never re-paid).
    """
    if amount <= 0:
        raise ValueError("A hold reserves a positive amount")
    _aware(expires_at, "expires_at")
    with transaction.atomic():
        account = _lock_account(customer_id)
        existing = CashbackHold.objects.filter(order_id=order_id).order_by("-id").first()
        if existing is not None:
            if existing.state == enums.CashbackHoldState.RELEASED:
                raise InvalidTransition(f"Hold of order {order_id} was released")
            return _hold_result(existing, changed=False)
        available = account.balance - selectors.held_amount(account.pk)
        if amount > available:
            raise InsufficientBalance(available=available, requested=amount)
        hold = CashbackHold.objects.create(
            account=account, order_id=order_id, amount=amount, expires_at=expires_at
        )
        return _hold_result(hold, changed=True)


def _latest_hold(order_id: int) -> CashbackHold | None:
    active = CashbackHold.objects.filter(order_id=order_id, state=enums.CashbackHoldState.ACTIVE).first()
    return active or CashbackHold.objects.filter(order_id=order_id).order_by("-id").first()


def capture_hold(order_id: int, *, at: datetime | None = None) -> HoldResult | None:
    """Payment confirmed: post the debit (``−amount``) and mark the order's hold ``captured``.

    ``None`` when the order has no hold (it used no cashback); an already captured hold is a no-op; a
    released one raises ``InvalidTransition``. ``at`` is the confirmation instant of the debit entry.
    """
    _aware(at, "at")
    with transaction.atomic():
        found = _latest_hold(order_id)
        if found is None:
            return None
        _lock_account_by_id(found.account_id)
        hold = CashbackHold.objects.get(pk=found.pk)  # re-read under the lock
        if hold.state == enums.CashbackHoldState.CAPTURED:
            return _hold_result(hold, changed=False)
        if hold.state == enums.CashbackHoldState.RELEASED:
            raise InvalidTransition(f"Hold of order {order_id} was released")
        posted = _post_entry(
            hold.account_id,
            kind=enums.LedgerKind.DEBIT,
            amount=-hold.amount,
            source_type=enums.LedgerSourceType.ORDER,
            order_id=order_id,
            source_ref=str(order_id),
            at=at,
        )
        CashbackHold.objects.filter(pk=hold.pk).update(
            state=enums.CashbackHoldState.CAPTURED,
            captured_entry_id=posted.entry_id,
            captured_at=posted.at,
            updated_at=timeutils.now(),
        )
        hold.refresh_from_db()
        return _hold_result(hold, changed=True)


def _release(hold: CashbackHold, reason: str, at: datetime | None) -> HoldResult:
    """Mark a hold read under its account lock ``released`` (no ledger entry)."""
    CashbackHold.objects.filter(pk=hold.pk).update(
        state=enums.CashbackHoldState.RELEASED,
        released_at=at or timeutils.now(),
        release_reason=reason,
        updated_at=timeutils.now(),
    )
    hold.refresh_from_db()
    return _hold_result(hold, changed=True)


def release_hold(order_id: int, reason: str, *, at: datetime | None = None) -> HoldResult | None:
    """Release the order's active hold (no ledger entry). ``reason`` is a ``CashbackHold.ReleaseReason``.

    ``None`` when the order has no hold; an already released hold is a no-op; a captured one raises
    ``InvalidTransition`` (a paid order is reversed through :func:`reverse_purchase` instead). The commerce
    reservation-expiry job calls this per order inside its own Order lock (02 §1.5), with
    ``reason="reservation_expired"``.
    """
    if reason not in CashbackHold.ReleaseReason.values:
        raise ValueError(f"Unknown release reason {reason!r}")
    _aware(at, "at")
    with transaction.atomic():
        found = _latest_hold(order_id)
        if found is None:
            return None
        _lock_account_by_id(found.account_id)
        hold = CashbackHold.objects.get(pk=found.pk)
        if hold.state == enums.CashbackHoldState.RELEASED:
            return _hold_result(hold, changed=False)
        if hold.state == enums.CashbackHoldState.CAPTURED:
            raise InvalidTransition(f"Hold of order {order_id} was captured")
        return _release(hold, reason, at)


def expire_holds(now: datetime | None = None) -> list[int]:
    """Maintenance/test helper: release every active hold whose ``expires_at`` has passed; order ids.

    **Not scheduled and not for production flows**: it takes only the account lock, never the Order lock, so
    it could release a hold just before the payment webhook captures it. Expiry belongs to the commerce job
    (02 §1.5), which calls :func:`release_hold` per order under its Order lock. Each hold is re-read under its
    account lock and skipped unless it is still ``active`` and due (a hold captured meanwhile is left alone,
    and the remaining holds are still processed); one transaction per hold.
    """
    _aware(now, "now")
    instant = now or timeutils.now()
    due = list(
        CashbackHold.objects.filter(state=enums.CashbackHoldState.ACTIVE, expires_at__lte=instant)
        .order_by("expires_at", "id")
        .values_list("id", "account_id", "order_id")
    )
    released: list[int] = []
    for hold_id, account_id, order_id in due:
        with transaction.atomic():
            _lock_account_by_id(account_id)
            hold = CashbackHold.objects.filter(
                pk=hold_id, state=enums.CashbackHoldState.ACTIVE, expires_at__lte=instant
            ).first()
            if hold is None:
                continue
            _release(hold, CashbackHold.ReleaseReason.RESERVATION_EXPIRED, instant)
        released.append(order_id)
    return released


# --- Purchases (design 02 §1.6, §1.9.1) ---


def _record_purchase(
    account: LoyaltyAccount,
    *,
    source: str,
    order_id: int | None,
    billz_order_uuid: UUID | None,
    billz_sale_id: str,
    billz_sale_ref: str,
    amount: int,
    promo_amount: int,
    cashback_paid: int,
    purchase_at: datetime,
    entry_at: datetime | None,
    items: Sequence[PurchaseLine],
    policy_version: int | None,
    source_type: str,
    source_ref: str,
) -> PurchaseResult:
    """Steps 1–7 of design 02 §1.6 under the account lock the caller holds."""
    if amount < 0 or promo_amount < 0 or cashback_paid < 0 or promo_amount > amount or cashback_paid > amount:
        raise ValueError("Purchase amounts must satisfy 0 ≤ promo, cashback_paid ≤ amount")
    lifetime_before = selectors.customer_totals(account.customer_id).lifetime
    if policy_version is None:
        policy = policy_at(purchase_at)
    else:
        policy = CashbackPolicy.objects.prefetch_related("tiers").get(version=policy_version)
    result = engine.credit_for_purchase(
        amount=amount,
        promo_amount=promo_amount,
        cashback_paid=cashback_paid,
        lifetime_before=lifetime_before,
        policy=selectors.rules_of(policy),
    )
    purchase = Purchase.objects.create(
        customer_id=account.customer_id,
        source=source,
        order_id=order_id,
        billz_sale_id=billz_sale_id,
        billz_sale_ref=billz_sale_ref,
        billz_order_uuid=billz_order_uuid,
        amount=amount,
        promo_amount=promo_amount,
        cashback_paid=cashback_paid,
        lifetime_before=lifetime_before,
        policy=policy,
        cashback_amount=0,
        at=purchase_at,
    )
    PurchaseItem.objects.bulk_create(
        PurchaseItem(
            purchase=purchase,
            position=position,
            product_id=line.product_id,
            billz_product_id=line.billz_product_id,
            name=line.name,
            quantity=line.quantity,
            unit_price=line.unit_price,
            promo=line.promo,
            line_total=line.line_total,
        )
        for position, line in enumerate(items)
    )
    credited_entry_id: int | None = None
    if result.credit > 0 and policy is not None:
        posted = _post_entry(
            account.pk,
            kind=enums.LedgerKind.CREDIT,
            amount=result.credit,
            source_type=source_type,
            order_id=order_id,
            purchase_id=purchase.pk,
            source_ref=source_ref,
            base=result.eligible,
            percent=result.percent,
            promo_excluded=result.promo_excluded,
            policy_id=policy.pk,
            at=entry_at,
        )
        credited_entry_id = posted.entry_id
        # both columns in ONE statement: purchase_credit_link is checked per statement
        Purchase.objects.filter(pk=purchase.pk).update(
            cashback_amount=result.credit, credited_entry_id=credited_entry_id, updated_at=timeutils.now()
        )
    return PurchaseResult(
        purchase_id=purchase.pk,
        customer_id=account.customer_id,
        source=source,
        order_id=order_id,
        billz_order_uuid=billz_order_uuid,
        billz_sale_id=billz_sale_id,
        billz_sale_ref=billz_sale_ref,
        amount=amount,
        promo_amount=promo_amount,
        cashback_paid=cashback_paid,
        lifetime_before=lifetime_before,
        policy_version=None if policy is None else policy.version,
        base=result.eligible,
        percent=result.percent,
        promo_excluded=result.promo_excluded,
        cashback_amount=result.credit,
        credited_entry_id=credited_entry_id,
        at=purchase_at,
        created=True,
    )


def _violated_constraint(exc: IntegrityError) -> str:
    """The constraint/index name PostgreSQL reports for a unique violation ('' when unknown)."""
    diag = getattr(exc.__cause__, "diag", None)
    return str(getattr(diag, "constraint_name", None) or "")


def _replay(existing: Purchase, customer_id: int, conflict: PurchaseConflict) -> PurchaseResult:
    """An idempotent redelivery returns the stored purchase; one that names another customer is a conflict."""
    if existing.customer_id != customer_id:
        raise conflict
    return _purchase_result(existing, created=False)


def record_online_purchase(
    customer_id: int,
    order_id: int,
    amount: int,
    promo_amount: int,
    cashback_paid: int,
    at: datetime,
    *,
    items: Sequence[PurchaseLine] = (),
    policy_version: int | None = None,
    billz_order_uuid: UUID | None = None,
) -> PurchaseResult:
    """A paid online order becomes a purchase, credited under the policy active at ``at``.

    Called after ``capture_hold`` inside the orchestrator's Order → Payment → LoyaltyAccount transaction.
    ``amount`` is ``items_subtotal`` (delivery fee never earns), ``cashback_paid`` is ``cashback_used``.
    Idempotent per order (``Purchase.order`` is 1:1): a replay for the same customer returns the existing
    purchase (``created=False``, otherwise equal to the first result); an order (or ``billz_order_uuid``)
    that already belongs to another customer's purchase raises :class:`PurchaseConflict`.
    ``billz_order_uuid`` (default ``uuid4()``) is persisted here, **before** the outbox ``billz.push_sale``
    row (dedupe key ``billz.push_sale:{billz_order_uuid}``) is committed.
    """
    _aware(at, "at")
    conflict = PurchaseConflict(f"Order {order_id} already belongs to another customer's purchase")
    with transaction.atomic():
        account = _lock_account(customer_id)
        existing = _load_purchase(order_id=order_id)
        if existing is not None:
            return _replay(existing, customer_id, conflict)
        try:
            with transaction.atomic():
                return _record_purchase(
                    account,
                    source=enums.PurchaseSource.ONLINE,
                    order_id=order_id,
                    billz_order_uuid=billz_order_uuid or uuid4(),
                    billz_sale_id="",
                    billz_sale_ref="",
                    amount=amount,
                    promo_amount=promo_amount,
                    cashback_paid=cashback_paid,
                    purchase_at=at,
                    entry_at=at,
                    items=items,
                    policy_version=policy_version,
                    source_type=enums.LedgerSourceType.ORDER,
                    source_ref=str(order_id),
                )
        except IntegrityError as exc:  # a concurrent writer committed the same order / Billz order id
            constraint = _violated_constraint(exc)
            if constraint == "purchase_billz_uuid_unique":
                raise PurchaseConflict(
                    f"Billz order {billz_order_uuid} already belongs to a purchase"
                ) from exc
            raced = (
                _load_purchase(order_id=order_id) if constraint == "loyalty_purchase_order_id_key" else None
            )
            if raced is None:
                raise
            return _replay(raced, customer_id, conflict)


def record_store_purchase(
    customer_id: int,
    billz_sale_id: str,
    billz_sale_ref: str,
    amount: int,
    sold_at: datetime,
    items: Sequence[PurchaseLine] = (),
    *,
    promo_amount: int = 0,
    at: datetime | None = None,
    policy_version: int | None = None,
) -> PurchaseResult:
    """An in-store Billz sale resolved through the personal QR (``source_type = billz_sale``).

    No in-store redemption (L6: ``cashback_paid = 0``). The purchase keeps the Billz sale time (``at =
    sold_at``, which also picks the policy); the credit entry is posted at ``at`` (default now). Idempotent
    by ``billz_sale_id`` (unique, ≤ 64 characters): a redelivery for the same customer returns the existing
    purchase, a sale that belongs to another customer raises :class:`BillzSaleConflict` (also when the
    other writer commits between the check and the insert). ``billz_sale_ref`` (≤ 32) defaults to the sale
    id; a longer id needs an explicit ref (``ValueError``).
    """
    if not billz_sale_id:
        raise ValueError("A store purchase needs the Billz sale id")
    ref = billz_sale_ref or billz_sale_id
    _check_billz_refs(billz_sale_id, ref)
    _aware(sold_at, "sold_at")
    _aware(at, "at")
    conflict = BillzSaleConflict(f"Billz sale {billz_sale_id} already belongs to another customer")
    with transaction.atomic():
        account = _lock_account(customer_id)
        existing = _load_purchase(billz_sale_id=billz_sale_id)
        if existing is not None:
            return _replay(existing, customer_id, conflict)
        try:
            with transaction.atomic():
                return _record_purchase(
                    account,
                    source=enums.PurchaseSource.STORE_QR,
                    order_id=None,
                    billz_order_uuid=None,
                    billz_sale_id=billz_sale_id,
                    billz_sale_ref=ref,
                    amount=amount,
                    promo_amount=promo_amount,
                    cashback_paid=0,
                    purchase_at=sold_at,
                    entry_at=at,
                    items=items,
                    policy_version=policy_version,
                    source_type=enums.LedgerSourceType.BILLZ_SALE,
                    source_ref=ref,
                )
        except IntegrityError as exc:  # another customer's sale, or an online echo, committed meanwhile
            if _violated_constraint(exc) != "purchase_billz_sale_unique":
                raise
            raced = _load_purchase(billz_sale_id=billz_sale_id)
            if raced is None:  # pragma: no cover - the violating row is committed, so it is visible
                raise
            return _replay(raced, customer_id, conflict)


def reverse_purchase(purchase_id: int, at: datetime, actor_id: int | None = None) -> ReversalResult:
    """Order cancelled after payment (design 02 §1.10): flag the purchase and reverse its entries.

    One ``reversal`` (``amount = −entry.amount``, unique ``reverses``) per unreversed ``credit``/``debit`` of
    the purchase's order (or Billz sale); the reversal of ``credited_entry`` is stored as
    ``Purchase.reversal_entry`` — also when that credit was reversed earlier by another path (the existing
    reversal is linked, so reconciliation check C5 stays clean). Idempotent: a second call posts nothing
    (``already_reversed=True``). The balance may go negative (L5): the ``loyalty.balance_negative`` audit
    then names ``actor_id`` (the cancelling admin); reversal rows themselves carry no actor.
    """
    _aware(at, "at")
    actor = _staff(actor_id)
    with transaction.atomic():
        found = Purchase.objects.filter(pk=purchase_id).only("id", "customer_id").first()
        if found is None:
            raise PurchaseNotFound(f"Purchase {purchase_id} does not exist")
        account = _lock_account(found.customer_id)
        purchase = Purchase.objects.get(pk=purchase_id)  # re-read under the lock
        if purchase.order_id is not None:
            entries = LedgerEntry.objects.filter(order_id=purchase.order_id)
        else:
            entries = LedgerEntry.objects.filter(purchase_id=purchase.pk)
        entries = entries.filter(
            kind__in=[enums.LedgerKind.CREDIT, enums.LedgerKind.DEBIT], reversed_by__isnull=True
        ).order_by("id")
        reversal_ids: list[int] = []
        credit_reversal: int | None = None
        net = 0
        for entry in entries:
            posted = _post_entry(
                account.pk,
                kind=enums.LedgerKind.REVERSAL,
                amount=-entry.amount,
                source_type=entry.source_type,
                order_id=entry.order_id,
                purchase_id=entry.purchase_id,
                source_ref=entry.source_ref,
                reverses_id=entry.pk,
                at=at,
                audit_actor=actor,
            )
            reversal_ids.append(posted.entry_id)
            net += posted.amount
            if entry.pk == purchase.credited_entry_id:
                credit_reversal = posted.entry_id
        if (
            credit_reversal is None
            and purchase.credited_entry_id is not None
            and purchase.reversal_entry_id is None
        ):
            credit_reversal = (
                LedgerEntry.objects.filter(reverses_id=purchase.credited_entry_id)
                .values_list("id", flat=True)
                .first()
            )
        already = purchase.reversed
        updates: dict[str, Any] = {}
        if not already:
            updates.update(reversed=True, reversed_at=at)
        if credit_reversal is not None:
            updates["reversal_entry_id"] = credit_reversal
        if updates:
            Purchase.objects.filter(pk=purchase.pk).update(**updates, updated_at=timeutils.now())
        return ReversalResult(
            purchase_id=purchase.pk,
            reversed_at=purchase.reversed_at if already and purchase.reversed_at is not None else at,
            reversal_entry_ids=tuple(reversal_ids),
            credit_reversal_entry_id=credit_reversal,
            net_amount=net,
            already_reversed=already,
        )


def record_billz_order(
    purchase_id: int, *, billz_order_uuid: UUID, billz_sale_id: str, billz_sale_ref: str
) -> LinkResult:
    """Persist the sale Billz just created for an online purchase (design "Billz sale"), nothing more.

    The live push (``integrations.billz``) calls this through the ``billz.push_sale`` handler the moment
    Billz answered the create, **before** any item is sent, so a retry after a crash at any later step resumes
    on that sale instead of creating another one. ``billz_pushed_at`` stays null until the sale is paid in
    Billz (:func:`link_billz_sale`). Same checks and idempotency as :func:`link_billz_sale`: the same id again
    is a no-op; another id on the purchase, an id of another purchase or a wrong ``billz_order_uuid`` raise
    :class:`BillzSaleConflict`.
    """
    if not billz_sale_id:
        raise ValueError("record_billz_order needs the Billz sale id")
    return _link_billz(
        purchase_id,
        billz_order_uuid=billz_order_uuid,
        billz_sale_id=billz_sale_id,
        billz_sale_ref=billz_sale_ref,
        pushed_at=None,
        mark_pushed=False,
    )


def link_billz_sale(
    purchase_id: int,
    *,
    billz_order_uuid: UUID | None = None,
    billz_sale_id: str = "",
    billz_sale_ref: str = "",
    pushed_at: datetime | None = None,
) -> LinkResult:
    """Record the Billz side of an online purchase (``billz.push_sale`` handler, M3 sync echo); never credits.

    The Billz order id (``billz_order_uuid``) was persisted by :func:`record_online_purchase` **before** the
    outbox row was committed (02 §1.6); the handler passes it back so a mismatch (a payload that does not
    belong to this purchase) raises :class:`BillzSaleConflict` instead of linking the wrong sale. A successful
    push sets ``billz_pushed_at`` (the handler's skip guard, 03 §5) even when Billz answers without a sale id;
    ``billz_sale_id`` (≤ 64)/``billz_sale_ref`` (≤ 32) are stored when known (the ref defaults to the sale
    id, so a longer id needs an explicit ref; ``ValueError`` otherwise). Idempotent:
    a pushed purchase with the same (or no new) sale id is a no-op (``changed=False``); a different sale id on
    an already linked purchase, or a sale id that already belongs to another purchase, raises
    :class:`BillzSaleConflict`.
    """
    return _link_billz(
        purchase_id,
        billz_order_uuid=billz_order_uuid,
        billz_sale_id=billz_sale_id,
        billz_sale_ref=billz_sale_ref,
        pushed_at=pushed_at,
        mark_pushed=True,
    )


def record_billz_payment_attempt(purchase_id: int, *, billz_sale_id: str, at: datetime) -> None:
    """Persist that the payment of the online purchase's Billz sale is about to be sent (review B2).

    Called by the ``billz.push_sale`` journal right before ``POST /v2/order-payment`` and committed before
    it, so an answer lost in transit is never followed by a second payment: the next attempt finds the stamp
    and re-reads the sale instead. Refuses (:class:`BillzSaleConflict`) a purchase that is not online, holds
    another sale or was pushed already.
    """
    _aware(at, "at")
    with transaction.atomic():
        purchase = _locked_online_purchase(purchase_id, billz_sale_id)
        if purchase.billz_pushed_at is not None:
            raise BillzSaleConflict(f"Purchase {purchase_id} was pushed to Billz already")
        Purchase.objects.filter(pk=purchase.pk).update(
            billz_payment_attempted_at=at, updated_at=timeutils.now()
        )


def clear_billz_payment_attempt(purchase_id: int, *, billz_sale_id: str) -> None:
    """Billz certainly did not take the payment (refused it, or it was never sent): forget the attempt."""
    with transaction.atomic():
        purchase = _locked_online_purchase(purchase_id, billz_sale_id)
        if purchase.billz_payment_attempted_at is not None and purchase.billz_pushed_at is None:
            Purchase.objects.filter(pk=purchase.pk).update(
                billz_payment_attempted_at=None, updated_at=timeutils.now()
            )


def _locked_online_purchase(purchase_id: int, billz_sale_id: str) -> Purchase:
    purchase = Purchase.objects.select_for_update(of=("self",)).filter(pk=purchase_id).first()
    if purchase is None:
        raise PurchaseNotFound(f"Purchase {purchase_id} does not exist")
    if purchase.source != enums.PurchaseSource.ONLINE:
        raise BillzSaleConflict(f"Purchase {purchase_id} is a store sale, not an online order")
    if not billz_sale_id or purchase.billz_sale_id != billz_sale_id:
        raise BillzSaleConflict(f"Purchase {purchase_id} is not linked to Billz sale {billz_sale_id!r}")
    return purchase


def _link_billz(
    purchase_id: int,
    *,
    billz_order_uuid: UUID | None,
    billz_sale_id: str,
    billz_sale_ref: str,
    pushed_at: datetime | None,
    mark_pushed: bool,
) -> LinkResult:
    _check_billz_refs(billz_sale_id, billz_sale_ref)
    _aware(pushed_at, "pushed_at")
    with transaction.atomic():
        purchase = Purchase.objects.select_for_update(of=("self",)).filter(pk=purchase_id).first()
        if purchase is None:
            raise PurchaseNotFound(f"Purchase {purchase_id} does not exist")
        if purchase.source != enums.PurchaseSource.ONLINE:
            raise BillzSaleConflict(f"Purchase {purchase_id} is a store sale, not an online order")
        if billz_order_uuid is not None and billz_order_uuid != purchase.billz_order_uuid:
            raise BillzSaleConflict(
                f"Purchase {purchase_id} was pushed as Billz order {purchase.billz_order_uuid}, "
                f"not {billz_order_uuid}"
            )
        if billz_sale_id and purchase.billz_sale_id and purchase.billz_sale_id != billz_sale_id:
            raise BillzSaleConflict(
                f"Purchase {purchase_id} is already linked to Billz sale {purchase.billz_sale_id}"
            )
        sale_id = billz_sale_id or purchase.billz_sale_id
        ref = billz_sale_ref or purchase.billz_sale_ref or sale_id
        _check_billz_refs(sale_id, ref)  # a defaulted ref (the sale id) must fit too
        pushed = purchase.billz_pushed_at or ((pushed_at or timeutils.now()) if mark_pushed else None)
        changed = (sale_id, ref, pushed) != (
            purchase.billz_sale_id,
            purchase.billz_sale_ref,
            purchase.billz_pushed_at,
        )
        if changed:
            try:
                with transaction.atomic():
                    Purchase.objects.filter(pk=purchase.pk).update(
                        billz_sale_id=sale_id,
                        billz_sale_ref=ref,
                        billz_pushed_at=pushed,
                        updated_at=timeutils.now(),
                    )
            except IntegrityError as exc:
                if "purchase_billz_sale_unique" not in str(exc):
                    raise
                raise BillzSaleConflict(f"Billz sale {sale_id} already belongs to another purchase") from exc
        return LinkResult(
            purchase_id=purchase.pk,
            billz_order_uuid=purchase.billz_order_uuid,
            billz_sale_id=sale_id,
            billz_sale_ref=ref,
            billz_pushed_at=pushed,
            changed=changed,
        )


# --- Adjustments (contract §2.6) ---


def _has_unstorable_characters(text: str) -> bool:
    """NUL or a lone surrogate: PostgreSQL text/JSONB cannot store either (DRF's CharField refuses both
    through ProhibitNullCharactersValidator / ProhibitSurrogateCharactersValidator)."""
    return any(char == "\x00" or "\ud800" <= char <= "\udfff" for char in text)


def _validate_adjustment(amount: Any, reason: Any) -> tuple[int, str]:
    errors: dict[str, list[ErrorDetail]] = {}
    if isinstance(amount, bool) or not isinstance(amount, int):
        errors["amount"] = [field_error("invalid", MESSAGE_AMOUNT)]
    elif amount < ADJUSTMENT_MIN_AMOUNT:
        errors["amount"] = [field_error("min_value", MESSAGE_AMOUNT)]
    elif amount > ADJUSTMENT_MAX_AMOUNT:
        errors["amount"] = [field_error("max_value", MESSAGE_AMOUNT)]
    text = reason.strip() if isinstance(reason, str) else ""
    if _has_unstorable_characters(text):
        errors["reason"] = [field_error("invalid")]
    elif len(text) < REASON_MIN_LENGTH:
        errors["reason"] = [field_error("min_length", limit=REASON_MIN_LENGTH)]
    elif len(text) > REASON_MAX_LENGTH:
        errors["reason"] = [field_error("max_length", limit=REASON_MAX_LENGTH)]
    if errors:
        raise AdjustmentValidationError(errors, MESSAGE_AMOUNT if "amount" in errors else MESSAGE_REASON)
    return int(amount), text


def adjust(
    customer_id: int,
    direction: str,
    amount: int,
    reason: str,
    actor_id: int,
    *,
    at: datetime | None = None,
    request: Any = None,
) -> AdjustmentResult:
    """Manual ``add``/``subtract`` by staff (contract §2.6 checks in order; allowed for deleted customers).

    ``amount`` 1..10 000 000 and ``reason`` 3..300 (trimmed) raise :class:`AdjustmentValidationError`;
    ``subtract`` with ``amount > available`` raises :class:`InsufficientBalance` under the account lock.
    Posts ``kind=adjustment, source_type=admin`` and audits ``cashback.adjusted``.
    """
    if direction not in enums.AdjustmentDirection.values:
        raise ValueError(f"Unknown adjustment direction {direction!r}")
    _aware(at, "at")
    amount, text = _validate_adjustment(amount, reason)
    staff = StaffUser.objects.get(pk=actor_id)
    with transaction.atomic():
        account = _lock_account(customer_id)
        held = selectors.held_amount(account.pk)
        signed = amount if direction == enums.AdjustmentDirection.ADD else -amount
        if signed < 0 and amount > account.balance - held:
            raise InsufficientBalance(available=account.balance - held, requested=amount)
        posted = _post_entry(
            account.pk,
            kind=enums.LedgerKind.ADJUSTMENT,
            amount=signed,
            source_type=enums.LedgerSourceType.ADMIN,
            reason=text,
            actor=staff,
            at=at,
            audit_actor=staff,
        )
        audit.record(
            action="cashback.adjusted",
            entity_type="customer",
            entity_id=str(customer_id),
            actor=staff,
            request=request,
            after={
                "direction": direction,
                "amount": amount,
                "reason": text,
                "balance_after": posted.balance_after,
                "ledger_entry_id": posted.entry_id,
            },
        )
        return AdjustmentResult(
            entry_id=posted.entry_id,
            customer_id=customer_id,
            account_id=account.pk,
            balance=posted.balance_after,
            held=held,
            available=posted.balance_after - held,
        )


# --- Policy versions (design 02 §1.1, contract §2.7) ---


def _tier_errors(tiers: Sequence[TierBand]) -> tuple[dict[str, list[ErrorDetail]], str | None]:
    """Contract §2.7 rows in order; every failing field, the top-level message of the first failing rule."""
    errors: dict[str, list[ErrorDetail]] = {}
    message: str | None = None

    def add(field: str, code: str, detail: str | None, top: str) -> None:
        nonlocal message
        errors.setdefault(field, []).append(field_error(code, detail))
        message = message or top

    if not tiers:
        errors["tiers"] = [field_error("min_items", MESSAGE_MIN_ITEMS)]
        return errors, MESSAGE_MIN_ITEMS
    for index, tier in enumerate(tiers):
        if tier.to_amount is not None and tier.to_amount <= tier.from_amount:
            add(f"tiers.{index}.to", "empty_range", MESSAGE_EMPTY_RANGE, MESSAGE_OVERLAP)
    ordered = sorted(
        enumerate(tiers), key=lambda pair: pair[1].from_amount
    )  # stable: submitted order on ties
    for (_, previous), (index, tier) in zip(ordered, ordered[1:], strict=False):
        if previous.to_amount is None or tier.from_amount < previous.to_amount:
            add(f"tiers.{index}.from", "overlap", MESSAGE_OVERLAP, MESSAGE_OVERLAP)
    for index, tier in enumerate(tiers):
        percent = tier.percent
        if not percent.is_finite() or not 0 < percent <= 100:
            add(f"tiers.{index}.percent", "out_of_range", MESSAGE_OUT_OF_RANGE, MESSAGE_OUT_OF_RANGE)
    for index, tier in enumerate(tiers):
        exponent = tier.percent.as_tuple().exponent
        if tier.percent.is_finite() and isinstance(exponent, int) and exponent < -2:
            add(f"tiers.{index}.percent", "max_decimal_places", None, MESSAGE_FIELDS)
    return errors, message


def tier_warnings(tiers: Sequence[TierBand]) -> tuple[TierWarning, ...]:
    """``gap`` for every uncovered ``[from, to)`` (also below the first tier), ``uncovered_above`` on top."""
    ordered = sorted(tiers, key=lambda t: t.from_amount)
    warnings: list[TierWarning] = []
    if ordered and ordered[0].from_amount > 0:
        warnings.append(TierWarning(enums.TierWarning.GAP, 0, ordered[0].from_amount))
    for previous, tier in zip(ordered, ordered[1:], strict=False):
        if previous.to_amount is not None and tier.from_amount > previous.to_amount:
            warnings.append(TierWarning(enums.TierWarning.GAP, previous.to_amount, tier.from_amount))
    if ordered and ordered[-1].to_amount is not None:
        warnings.append(TierWarning(enums.TierWarning.UNCOVERED_ABOVE, ordered[-1].to_amount, None))
    return tuple(warnings)


def validate_policy_version(
    effective_from: datetime, tiers: Sequence[TierBand], *, now: datetime
) -> tuple[TierWarning, ...]:
    """Raise :class:`PolicyValidationError` per contract §2.7; return the warnings of a valid submission."""
    _aware(effective_from, "effective_from")
    _aware(now, "now")
    errors, message = _tier_errors(tiers)
    if effective_from <= now:
        errors["effective_from"] = [field_error("effective_from_in_past", MESSAGE_IN_PAST)]
        message = message or MESSAGE_FIELDS
    if errors:
        raise PolicyValidationError(errors, message or MESSAGE_FIELDS)
    return tier_warnings(tiers)


def _audit_policy_created(
    policy: CashbackPolicy,
    tiers: Sequence[TierBand],
    author: StaffUser | None,
    superseded_version: int | None,
    request: Any,
) -> None:
    audit.record(
        action="cashback.policy_version_created",
        entity_type=AUDIT_POLICY_ENTITY,
        entity_id=str(policy.version),
        actor=author,
        request=request,
        after={
            "version": policy.version,
            "effective_from": timeutils.isoformat(policy.effective_from),
            "basis": policy.basis,
            "earn_on_cashback_paid": policy.earn_on_cashback_paid,
            "tiers": [
                {"from": t.from_amount, "to": t.to_amount, "percent": str(t.percent)}
                for t in sorted(tiers, key=lambda t: t.from_amount)
            ],
            "superseded_version": superseded_version,
        },
    )


def create_policy_version(
    effective_from: datetime,
    basis: str,
    earn_on_cashback_paid: bool,
    tiers: Sequence[TierBand],
    author_id: int | None,
    *,
    request: Any = None,
) -> PolicyVersionResult:
    """Append a policy version (contract §2.7): validate, then insert under an advisory lock.

    ``now`` is evaluated once, used for the ``effective_from_in_past`` check and stored as ``created_at`` so
    the DB check ``effective_from > created_at`` is the same comparison (a residual ``IntegrityError`` from
    clock skew maps to the same field error). A version that was ``scheduled`` becomes ``superseded`` by
    the derivation rule (audit ``cashback.policy_version_superseded``); the new one is audited as
    ``cashback.policy_version_created``.
    """
    if basis not in enums.RateBasis.values:
        raise ValueError(f"Unknown rate basis {basis!r}")
    _aware(effective_from, "effective_from")
    now = timeutils.now()
    warnings = validate_policy_version(effective_from, tiers, now=now)
    author = _staff(author_id)
    ordered = sorted(tiers, key=lambda t: t.from_amount)
    with transaction.atomic():
        with connection.cursor() as cursor:
            cursor.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", [POLICY_ADVISORY_LOCK])
        previously_scheduled = scheduled_policy(now)
        version = int(CashbackPolicy.objects.aggregate(top=Max("version"))["top"] or 0) + 1
        try:
            with transaction.atomic():
                policy = CashbackPolicy.objects.create(
                    version=version,
                    effective_from=effective_from,
                    basis=basis,
                    earn_on_cashback_paid=earn_on_cashback_paid,
                    author=author,
                    created_at=now,
                )
        except IntegrityError as exc:
            if "policy_effective_after_created" in str(exc):
                raise PolicyValidationError(
                    {"effective_from": [field_error("effective_from_in_past", MESSAGE_IN_PAST)]},
                    MESSAGE_FIELDS,
                ) from exc
            raise
        CashbackTier.objects.bulk_create(
            CashbackTier(
                policy=policy,
                from_amount=tier.from_amount,
                to_amount=tier.to_amount,
                percent=tier.percent.quantize(engine.PERCENT_QUANTUM),
                created_at=now,
            )
            for tier in ordered
        )
        superseded_version = None if previously_scheduled is None else previously_scheduled.version
        _audit_policy_created(policy, ordered, author, superseded_version, request)
        if previously_scheduled is not None:
            audit.record(
                action="cashback.policy_version_superseded",
                entity_type=AUDIT_POLICY_ENTITY,
                entity_id=str(previously_scheduled.version),
                actor=author,
                request=request,
                before={"state": enums.PolicyVersionState.SCHEDULED},
                after={"state": enums.PolicyVersionState.SUPERSEDED, "superseded_by": version},
            )
        return PolicyVersionResult(
            version=version, policy_id=policy.pk, superseded_version=superseded_version, warnings=warnings
        )


# --- POS devices (customer contract §2.10; plan §4 P3) ---

POS_KEY_PREFIX = "gdpos"
POS_PREFIX_LENGTH = 8
POS_SECRET_BYTES = 24  # secrets.token_urlsafe(24) → 32 base64url characters
POS_PREFIX_ALPHABET = "abcdefghijklmnopqrstuvwxyz0123456789"


class PosDeviceError(LoyaltyError):
    """A POS key command that cannot run (unknown actor or prefix, inactive device); the message is for the
    operator's terminal."""


def _pos_actor(actor_email: str) -> StaffUser:
    actor = StaffUser.objects.filter(email__iexact=actor_email.strip(), is_active=True).first()
    if actor is None:
        raise PosDeviceError(f"--actor must name an active staff user (got {actor_email!r})")
    return actor


def _pos_key(prefix: str) -> str:
    return f"{POS_KEY_PREFIX}_{prefix}_{secrets.token_urlsafe(POS_SECRET_BYTES)}"


def _new_pos_prefix() -> str:
    """A prefix no device uses yet; the unique constraint on ``key_prefix`` settles a concurrent twin."""
    while True:
        prefix = "".join(secrets.choice(POS_PREFIX_ALPHABET) for _ in range(POS_PREFIX_LENGTH))
        if not PosDevice.objects.filter(key_prefix=prefix).exists():
            return prefix


def pos_key_hash(raw_key: str) -> str:
    """The stored form of a POS key (SHA-256 hex); the raw key is never stored."""
    return hashlib.sha256(raw_key.encode("utf-8")).hexdigest()


def _pos_device(prefix: str) -> PosDevice:
    """The device with this key prefix (unique, ``loyalty.0003``), locked."""
    device = PosDevice.objects.select_for_update().filter(key_prefix=prefix.strip()).first()
    if device is None:
        raise PosDeviceError(f"No POS device with the key prefix {prefix!r}")
    return device


def _audit_pos(action: str, device: PosDevice, actor: StaffUser, **extra: Any) -> None:
    audit.record(
        action=action,
        entity_type="pos_device",
        entity_id=str(device.pk),
        actor=actor,
        after={"pos_device_id": device.pk, **extra},
    )


@transaction.atomic
def create_pos_device(name: str, location_label: str, actor_email: str) -> tuple[PosDevice, str]:
    """``manage.py createposkey``: a new active device and its raw key (shown once, stored as SHA-256).
    Audit ``pos.device_created {pos_device_id, name}`` with the staff actor."""
    actor = _pos_actor(actor_email)
    name = name.strip()
    if not name:
        raise PosDeviceError("--name is required")
    for attempt in range(3):
        prefix = _new_pos_prefix()
        raw_key = _pos_key(prefix)
        try:
            with transaction.atomic():
                device = PosDevice.objects.create(
                    name=name[:100],
                    location_label=location_label.strip()[:100],
                    key_prefix=prefix,
                    key_hash=pos_key_hash(raw_key),
                    created_by=actor,
                )
        except IntegrityError:  # a concurrent createposkey drew the same prefix
            if attempt == 2:
                raise
            continue
        _audit_pos("pos.device_created", device, actor, name=device.name)
        return device, raw_key
    raise AssertionError("unreachable: the last attempt re-raises")


@transaction.atomic
def revoke_pos_device(prefix: str, actor_email: str) -> PosDevice:
    """``manage.py revokeposkey``: ``is_active=False`` (the next call answers 401); audit
    ``pos.device_revoked``. Revoking a revoked device changes nothing and audits nothing."""
    actor = _pos_actor(actor_email)
    device = _pos_device(prefix)
    if device.is_active:
        device.is_active = False
        device.save(update_fields=["is_active", "updated_at"])
        _audit_pos("pos.device_revoked", device, actor)
    return device


@transaction.atomic
def rotate_pos_key(prefix: str, actor_email: str) -> str:
    """``manage.py rotateposkey``: a new key with the same prefix replaces the old hash (the old key answers
    401 at once); returns the raw key (shown once). Audit ``pos.device_rotated``. A revoked device is not
    rotated."""
    actor = _pos_actor(actor_email)
    device = _pos_device(prefix)
    if not device.is_active:
        raise PosDeviceError(f"The POS device {prefix!r} is revoked")
    raw_key = _pos_key(device.key_prefix)
    device.key_hash = pos_key_hash(raw_key)
    device.save(update_fields=["key_hash", "updated_at"])
    _audit_pos("pos.device_rotated", device, actor)
    return raw_key


def ensure_pos_device(name: str, raw_key: str, actor: StaffUser | None) -> bool:
    """Insert a device for a known key unless its hash or its key prefix exists (the local/test
    ``POS_API_KEYS`` seeding); returns whether a row was created."""
    digest = pos_key_hash(raw_key)
    if PosDevice.objects.filter(key_hash=digest).exists():
        return False
    prefix = raw_key.split("_")[1][:12]
    if PosDevice.objects.filter(key_prefix=prefix).exists():
        # a second device on one prefix would make revokeposkey/rotateposkey ambiguous
        logger.warning("pos.seed_prefix_in_use prefix=%s name=%s", prefix, name[:100])
        return False
    PosDevice.objects.create(name=name[:100], key_prefix=prefix, key_hash=digest, created_by=actor)
    return True


__all__ = [
    "ADJUSTMENT_MAX_AMOUNT",
    "ADJUSTMENT_MIN_AMOUNT",
    "AdjustmentResult",
    "AdjustmentValidationError",
    "BillzSaleConflict",
    "HoldResult",
    "InsufficientBalance",
    "InvalidTransition",
    "LinkResult",
    "LoyaltyError",
    "LoyaltyValidationError",
    "OutOfOrderPosting",
    "PolicyValidationError",
    "PosDeviceError",
    "PolicyVersionResult",
    "PostedEntry",
    "PurchaseConflict",
    "PurchaseLine",
    "PurchaseNotFound",
    "PurchaseResult",
    "ReversalResult",
    "TierBand",
    "TierWarning",
    "active_policy",
    "adjust",
    "capture_hold",
    "clear_billz_payment_attempt",
    "create_hold",
    "create_policy_version",
    "create_pos_device",
    "ensure_pos_device",
    "expire_holds",
    "get_or_create_account",
    "link_billz_sale",
    "next_effective_from",
    "policy_at",
    "pos_key_hash",
    "post_entry",
    "record_billz_order",
    "record_billz_payment_attempt",
    "record_online_purchase",
    "record_store_purchase",
    "release_hold",
    "reverse_purchase",
    "revoke_pos_device",
    "rotate_pos_key",
    "scheduled_policy",
    "tier_warnings",
    "validate_policy_version",
]
