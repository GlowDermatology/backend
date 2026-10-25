"""Models of `loyalty` — docs/plans/m1-models/02-… §1 (slice B2).

``CashbackPolicy``/``CashbackTier`` are immutable rule versions (append-only; ``state``, ``effective_to`` and
``superseded_by`` are derived, never stored), ``LoyaltyAccount`` is the customer's balance (changed only by
``loyalty.services.post_entry`` in B4a), ``LedgerEntry`` the append-only signed movements with the balance
after each, ``CashbackHold`` cashback reserved for an unpaid order, ``Purchase``/``PurchaseItem`` the
lifetime-total rows with the engine's inputs and result snapshotted, and ``PosDevice`` a cashier terminal
allowed to resolve the personal QR (M2). Every write goes through ``loyalty.services``; the Django admin is
read-only.
"""

from __future__ import annotations

from datetime import datetime
from typing import ClassVar, Self

from django.db import models
from django.db.models import Exists, F, OuterRef, Q, Subquery

from glowderma.core import enums, timeutils
from glowderma.core.models import AppendOnlyModel, AppendOnlyQuerySet, TimeStampedModel, timestamp_default


class CashbackPolicyQuerySet(AppendOnlyQuerySet["CashbackPolicy"]):
    """Derived columns of a policy version (design 02 §1.1, the only supersession rule).

    ``superseded_by(v)`` = the smallest ``w.version`` with ``w.version > v.version AND w.created_at <
    v.effective_from`` (a later version created while ``v`` was still scheduled replaces it);
    ``effective_to(v)`` = ``MIN(n.effective_from)`` over the non-superseded ``n`` with
    ``n.version > v.version``.
    """

    def _superseder_of(self, outer: str = "version") -> models.QuerySet[CashbackPolicy]:
        return self.model.objects.filter(
            version__gt=OuterRef(outer), created_at__lt=OuterRef("effective_from")
        ).order_by()

    def with_derived(self) -> Self:
        superseded_by = Subquery(self._superseder_of().order_by("version").values("version")[:1])
        later_non_superseded = (
            self.model.objects.filter(version__gt=OuterRef("version"))
            .annotate(is_superseded=Exists(self._superseder_of()))
            .filter(is_superseded=False)
            .order_by("effective_from")
            .values("effective_from")[:1]
        )
        return self.annotate(superseded_by=superseded_by, effective_to=Subquery(later_non_superseded))

    def active_at(self, instant: datetime) -> CashbackPolicy | None:
        """The version in force at ``instant`` (``active_policy_at``): newest started, not superseded."""
        return (
            self.filter(effective_from__lte=instant)
            .annotate(is_superseded=Exists(self._superseder_of()))
            .filter(is_superseded=False)
            .order_by("-version")
            .first()
        )


CashbackPolicyManager = models.Manager.from_queryset(CashbackPolicyQuerySet)


class CashbackPolicy(AppendOnlyModel):
    """One immutable version of the cashback rules; its tiers are the bands of ``CashbackTier``."""

    version = models.PositiveIntegerField(unique=True)
    effective_from = models.DateTimeField()
    basis = models.CharField(max_length=16, choices=enums.RateBasis.choices)  # before_purchase|with_purchase
    earn_on_cashback_paid = models.BooleanField()
    author = models.ForeignKey(
        "accounts.StaffUser", null=True, on_delete=models.PROTECT, related_name="cashback_policies"
    )  # null = system / seed ("Tizim")
    created_at = models.DateTimeField(default=timestamp_default, editable=False)

    objects: ClassVar[CashbackPolicyManager[CashbackPolicy]] = CashbackPolicyManager()

    class Meta:
        ordering = ["-version"]
        constraints = [
            models.CheckConstraint(
                condition=Q(effective_from__gt=F("created_at")), name="policy_effective_after_created"
            ),
        ]
        indexes = [models.Index(fields=["effective_from"], name="policy_effective_from_idx")]

    def __str__(self) -> str:
        return f"Cashback policy v{self.version} ({self.basis}, from {self.effective_from:%Y-%m-%d})"

    # Derived columns: ``with_derived()`` attaches them as annotations (Django assigns them through the
    # setters below); a plain instance loads both lazily with one query.
    def _load_derived(self) -> None:
        derived = CashbackPolicy.objects.with_derived().get(pk=self.pk)
        self.__dict__["_superseded_by"] = derived.__dict__["_superseded_by"]
        self.__dict__["_effective_to"] = derived.__dict__["_effective_to"]

    @property
    def superseded_by(self) -> int | None:
        if "_superseded_by" not in self.__dict__:
            self._load_derived()
        value: int | None = self.__dict__["_superseded_by"]
        return value

    @superseded_by.setter
    def superseded_by(self, value: int | None) -> None:
        self.__dict__["_superseded_by"] = value

    @property
    def effective_to(self) -> datetime | None:
        if "_effective_to" not in self.__dict__:
            self._load_derived()
        value: datetime | None = self.__dict__["_effective_to"]
        return value

    @effective_to.setter
    def effective_to(self, value: datetime | None) -> None:
        self.__dict__["_effective_to"] = value

    def state_at(self, now: datetime) -> str:
        """``PolicyVersionState`` at ``now`` from the two derived columns (design 02 §1.1)."""
        if self.superseded_by is not None:
            return enums.PolicyVersionState.SUPERSEDED
        if self.effective_from > now:
            return enums.PolicyVersionState.SCHEDULED
        effective_to = self.effective_to
        if effective_to is None or effective_to > now:
            return enums.PolicyVersionState.ACTIVE
        return enums.PolicyVersionState.EXPIRED

    @property
    def state(self) -> str:
        return self.state_at(timeutils.now())


class CashbackTier(AppendOnlyModel):
    """One band ``[from_amount, to_amount)`` of a policy version with its percent."""

    policy = models.ForeignKey("loyalty.CashbackPolicy", on_delete=models.PROTECT, related_name="tiers")
    from_amount = models.BigIntegerField()  # inclusive, so'm
    to_amount = models.BigIntegerField(null=True)  # exclusive, so'm; null = open-ended
    percent = models.DecimalField(max_digits=5, decimal_places=2)
    created_at = models.DateTimeField(default=timestamp_default, editable=False)

    class Meta:
        ordering = ["policy_id", "from_amount"]
        constraints = [
            models.UniqueConstraint(fields=["policy", "from_amount"], name="tier_policy_from_unique"),
            models.CheckConstraint(condition=Q(from_amount__gte=0), name="tier_from_nonneg"),
            models.CheckConstraint(
                condition=Q(to_amount__isnull=True) | Q(to_amount__gt=F("from_amount")),
                name="tier_to_above_from",
            ),
            models.CheckConstraint(condition=Q(percent__gt=0, percent__lte=100), name="tier_percent_range"),
        ]

    def __str__(self) -> str:
        upper = "∞" if self.to_amount is None else str(self.to_amount)
        return f"v{self.policy_id} [{self.from_amount}, {upper}) {self.percent}%"


class LoyaltyAccount(TimeStampedModel):
    """The customer's cashback balance; one row per customer, created lazily by ``get_or_create_account``."""

    customer = models.OneToOneField(
        "accounts.Customer", on_delete=models.PROTECT, related_name="loyalty_account"
    )
    balance = models.BigIntegerField(default=0)  # signed; negative allowed (L5) and flagged in selectors

    def __str__(self) -> str:
        return f"Loyalty account of customer {self.customer_id}: {self.balance}"


class LedgerEntry(AppendOnlyModel):
    """One immutable, signed movement of a customer's cashback balance with the balance after it."""

    account = models.ForeignKey("loyalty.LoyaltyAccount", on_delete=models.PROTECT, related_name="entries")
    kind = models.CharField(
        max_length=16, choices=enums.LedgerKind.choices
    )  # credit|debit|adjustment|reversal
    amount = models.BigIntegerField()  # signed so'm
    balance_after = models.BigIntegerField()
    source_type = models.CharField(
        max_length=16, choices=enums.LedgerSourceType.choices
    )  # order|billz_sale|admin
    order = models.ForeignKey(
        "commerce.Order", null=True, on_delete=models.PROTECT, related_name="ledger_entries"
    )
    purchase = models.ForeignKey(
        "loyalty.Purchase", null=True, on_delete=models.PROTECT, related_name="ledger_entries"
    )
    source_ref = models.CharField(
        max_length=64, blank=True
    )  # order → str(order_id); billz_sale → Billz document no.
    # credit-only snapshot of the engine inputs
    base = models.BigIntegerField(null=True)  # eligible amount the percent was applied to
    percent = models.DecimalField(max_digits=5, decimal_places=2, null=True)
    promo_excluded = models.BooleanField(default=False)  # promo items were excluded from the base
    policy = models.ForeignKey(
        "loyalty.CashbackPolicy", null=True, on_delete=models.PROTECT, related_name="ledger_entries"
    )
    # reversal-only
    reverses = models.OneToOneField("self", null=True, on_delete=models.PROTECT, related_name="reversed_by")
    # adjustment-only
    reason = models.TextField(blank=True)
    actor = models.ForeignKey(
        "accounts.StaffUser", null=True, on_delete=models.PROTECT, related_name="ledger_adjustments"
    )
    at = models.DateTimeField(default=timestamp_default, editable=False)  # business time; API "at"

    class Meta:
        ordering = ["-at", "-id"]
        indexes = [
            models.Index(fields=["account", "at", "id"], name="ledger_account_at_idx"),
            models.Index(fields=["order"], name="ledger_order_idx"),
            models.Index(fields=["purchase"], name="ledger_purchase_idx"),
        ]
        constraints = [
            models.CheckConstraint(
                condition=(
                    Q(kind=enums.LedgerKind.CREDIT, amount__gt=0)
                    | Q(kind=enums.LedgerKind.DEBIT, amount__lt=0)
                    | Q(kind__in=[enums.LedgerKind.ADJUSTMENT, enums.LedgerKind.REVERSAL]) & ~Q(amount=0)
                ),
                name="ledger_amount_sign",
            ),
            models.CheckConstraint(
                condition=(
                    Q(kind=enums.LedgerKind.REVERSAL, reverses__isnull=False)
                    | ~Q(kind=enums.LedgerKind.REVERSAL) & Q(reverses__isnull=True)
                ),
                name="ledger_reversal_link",
            ),
            models.CheckConstraint(
                condition=(
                    Q(
                        kind=enums.LedgerKind.CREDIT,
                        base__isnull=False,
                        base__gte=0,
                        percent__isnull=False,
                        policy__isnull=False,
                    )
                    | ~Q(kind=enums.LedgerKind.CREDIT)
                    & Q(base__isnull=True, percent__isnull=True, policy__isnull=True, promo_excluded=False)
                ),
                name="ledger_credit_fields",
            ),
            models.CheckConstraint(
                condition=(
                    Q(source_type=enums.LedgerSourceType.ADMIN, kind=enums.LedgerKind.ADJUSTMENT)
                    | ~Q(source_type=enums.LedgerSourceType.ADMIN) & ~Q(kind=enums.LedgerKind.ADJUSTMENT)
                ),
                name="ledger_admin_is_adjustment",
            ),
            models.CheckConstraint(
                condition=(
                    Q(source_type=enums.LedgerSourceType.ORDER, order__isnull=False) & ~Q(source_ref="")
                    | Q(
                        source_type=enums.LedgerSourceType.BILLZ_SALE,
                        order__isnull=True,
                        purchase__isnull=False,
                    )
                    & ~Q(source_ref="")
                    | Q(
                        source_type=enums.LedgerSourceType.ADMIN,
                        order__isnull=True,
                        purchase__isnull=True,
                        source_ref="",
                    )
                ),
                name="ledger_source_refs",
            ),
            models.CheckConstraint(
                condition=(
                    Q(kind=enums.LedgerKind.ADJUSTMENT, actor__isnull=False) & ~Q(reason="")
                    | ~Q(kind=enums.LedgerKind.ADJUSTMENT) & Q(actor__isnull=True, reason="")
                ),
                name="ledger_adjustment_fields",
            ),
            models.CheckConstraint(
                condition=Q(percent__isnull=True) | Q(percent__gt=0, percent__lte=100),
                name="ledger_percent_range",
            ),
        ]

    def __str__(self) -> str:
        return (
            f"Ledger #{self.pk} {self.kind} {self.amount:+} → {self.balance_after} (acct {self.account_id})"
        )


class CashbackHold(TimeStampedModel):
    """Cashback reserved at checkout for an unpaid order: captured into a debit on payment, else released."""

    class ReleaseReason(models.TextChoices):  # internal
        RESERVATION_EXPIRED = "reservation_expired"
        ORDER_CANCELLED = "order_cancelled"
        PAYMENT_FAILED = "payment_failed"

    account = models.ForeignKey("loyalty.LoyaltyAccount", on_delete=models.PROTECT, related_name="holds")
    order = models.ForeignKey("commerce.Order", on_delete=models.PROTECT, related_name="cashback_holds")
    amount = models.BigIntegerField()
    state = models.CharField(
        max_length=16, choices=enums.CashbackHoldState.choices, default=enums.CashbackHoldState.ACTIVE
    )
    expires_at = models.DateTimeField()  # = Order.reservation_expires_at at creation
    captured_entry = models.OneToOneField(
        "loyalty.LedgerEntry", null=True, on_delete=models.PROTECT, related_name="captured_hold"
    )
    captured_at = models.DateTimeField(null=True)
    released_at = models.DateTimeField(null=True)
    release_reason = models.CharField(max_length=24, blank=True, choices=ReleaseReason.choices)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["order"],
                condition=Q(state=enums.CashbackHoldState.ACTIVE),
                name="hold_one_active_per_order",
            ),
            models.CheckConstraint(condition=Q(amount__gt=0), name="hold_amount_positive"),
            models.CheckConstraint(
                condition=(
                    Q(
                        state=enums.CashbackHoldState.ACTIVE,
                        captured_entry__isnull=True,
                        captured_at__isnull=True,
                        released_at__isnull=True,
                        release_reason="",
                    )
                    | Q(
                        state=enums.CashbackHoldState.CAPTURED,
                        captured_entry__isnull=False,
                        captured_at__isnull=False,
                        released_at__isnull=True,
                        release_reason="",
                    )
                    | Q(
                        state=enums.CashbackHoldState.RELEASED,
                        captured_entry__isnull=True,
                        captured_at__isnull=True,
                        released_at__isnull=False,
                    )
                    & ~Q(release_reason="")
                ),
                name="hold_state_fields",
            ),
        ]
        indexes = [
            models.Index(
                fields=["account"],
                condition=Q(state=enums.CashbackHoldState.ACTIVE),
                name="hold_active_account_idx",
            ),
            models.Index(
                fields=["expires_at"],
                condition=Q(state=enums.CashbackHoldState.ACTIVE),
                name="hold_active_expires_idx",
            ),
        ]

    def __str__(self) -> str:
        return f"Hold {self.amount} on order {self.order_id} ({self.state})"


class Purchase(TimeStampedModel):
    """One purchase counting towards the lifetime total (paid online order or in-store Billz sale)."""

    customer = models.ForeignKey("accounts.Customer", on_delete=models.PROTECT, related_name="purchases")
    source = models.CharField(max_length=16, choices=enums.PurchaseSource.choices)  # online|store_qr
    order = models.OneToOneField(
        "commerce.Order", null=True, on_delete=models.PROTECT, related_name="purchase"
    )
    # Billz identifiers
    billz_sale_id = models.CharField(max_length=64, blank=True)  # Billz's own sale id (store, or online echo)
    billz_sale_ref = models.CharField(max_length=32, blank=True)  # human document number ("S-88213")
    billz_order_uuid = models.UUIDField(null=True)  # online: generated BEFORE the push, the Billz order id
    billz_pushed_at = models.DateTimeField(null=True)  # online: set when billz.push_sale succeeded
    # engine inputs (snapshots)
    amount = models.BigIntegerField()  # gross total before cashback redemption (items_subtotal online)
    promo_amount = models.BigIntegerField(default=0)  # part of amount that is promotional
    cashback_paid = models.BigIntegerField(default=0)  # part paid from the balance (= Order.cashback_used)
    lifetime_before = models.BigIntegerField()  # Σ non-reversed purchases of the customer before this one
    # engine result
    policy = models.ForeignKey(
        "loyalty.CashbackPolicy", null=True, on_delete=models.PROTECT, related_name="purchases"
    )  # version active at `at`; null = no policy existed
    cashback_amount = models.BigIntegerField(default=0)  # credited so'm (0 when no tier covers / eligible 0)
    credited_entry = models.OneToOneField(
        "loyalty.LedgerEntry", null=True, on_delete=models.PROTECT, related_name="credited_purchase"
    )
    # reversal
    reversed = models.BooleanField(default=False)
    reversed_at = models.DateTimeField(null=True)
    reversal_entry = models.OneToOneField(
        "loyalty.LedgerEntry", null=True, on_delete=models.PROTECT, related_name="reversed_purchase"
    )  # the reversal of credited_entry (null if nothing was credited)
    at = models.DateTimeField(default=timestamp_default)  # business time: payment confirmation / Billz sale

    class Meta:
        ordering = ["-at", "-id"]
        constraints = [
            models.UniqueConstraint(
                fields=["billz_sale_id"], condition=~Q(billz_sale_id=""), name="purchase_billz_sale_unique"
            ),
            models.UniqueConstraint(fields=["billz_order_uuid"], name="purchase_billz_uuid_unique"),
            models.CheckConstraint(
                condition=Q(
                    amount__gte=0,
                    promo_amount__gte=0,
                    cashback_paid__gte=0,
                    cashback_amount__gte=0,
                    lifetime_before__gte=0,
                    promo_amount__lte=F("amount"),
                    cashback_paid__lte=F("amount"),
                ),
                name="purchase_amounts",
            ),
            models.CheckConstraint(
                condition=(
                    Q(
                        source=enums.PurchaseSource.ONLINE,
                        order__isnull=False,
                        billz_order_uuid__isnull=False,
                    )
                    | Q(
                        source=enums.PurchaseSource.STORE_QR,
                        order__isnull=True,
                        billz_order_uuid__isnull=True,
                    )
                    & ~Q(billz_sale_id="")
                ),
                name="purchase_source_refs",
            ),
            models.CheckConstraint(
                condition=(
                    Q(reversed=True, reversed_at__isnull=False)
                    | Q(reversed=False, reversed_at__isnull=True, reversal_entry__isnull=True)
                ),
                name="purchase_reversed_fields",
            ),
            models.CheckConstraint(
                condition=(
                    Q(cashback_amount__gt=0, credited_entry__isnull=False)
                    | Q(cashback_amount=0, credited_entry__isnull=True, reversal_entry__isnull=True)
                ),
                name="purchase_credit_link",
            ),
        ]
        indexes = [
            models.Index(fields=["customer", "at", "id"], name="purchase_customer_at_idx"),
            models.Index(
                fields=["customer", "source"], condition=Q(reversed=False), name="purchase_live_cust_src_idx"
            ),
        ]

    def __str__(self) -> str:
        ref = (
            f"order {self.order_id}"
            if self.source == enums.PurchaseSource.ONLINE
            else f"Billz {self.billz_sale_ref or self.billz_sale_id}"
        )
        suffix = " reversed" if self.reversed else ""
        return f"Purchase #{self.pk} {self.source} {self.amount} ({ref}){suffix}"


class PurchaseItem(models.Model):
    """Line snapshot of a purchase (what was bought, at which unit price, promo or not)."""

    purchase = models.ForeignKey("loyalty.Purchase", on_delete=models.CASCADE, related_name="items")
    position = models.PositiveSmallIntegerField()
    product = models.ForeignKey(
        "catalog.Product", null=True, on_delete=models.PROTECT, related_name="purchase_items"
    )  # null for Billz lines not in the catalog
    billz_product_id = models.CharField(max_length=32, blank=True)
    name = models.CharField(max_length=200)  # uz display name at purchase time, or the Billz name
    quantity = models.PositiveIntegerField()
    unit_price = models.BigIntegerField()
    promo = models.BooleanField(default=False)
    line_total = models.BigIntegerField()  # quantity × unit_price

    class Meta:
        ordering = ["purchase_id", "position"]
        constraints = [
            models.UniqueConstraint(fields=["purchase", "position"], name="purchaseitem_position_unique"),
            models.CheckConstraint(
                condition=Q(quantity__gt=0, unit_price__gte=0, line_total__gte=0), name="purchaseitem_amounts"
            ),
        ]

    def __str__(self) -> str:
        return f"{self.name} × {self.quantity} @ {self.unit_price} (purchase {self.purchase_id})"


class PosDevice(TimeStampedModel):
    """A cashier terminal allowed to call ``POST /api/v1/pos/cashback/resolve-qr`` (M2); key stored hashed."""

    name = models.CharField(max_length=100)
    location_label = models.CharField(max_length=100, blank=True)  # free text; no FK to professionals
    key_prefix = models.CharField(max_length=12)  # first 8–12 chars of the key, for display/rotation
    key_hash = models.CharField(max_length=64, unique=True)  # SHA-256 hex of the full key
    is_active = models.BooleanField(default=True)
    last_used_at = models.DateTimeField(null=True)
    created_by = models.ForeignKey(
        "accounts.StaffUser", null=True, on_delete=models.PROTECT, related_name="pos_devices"
    )

    class Meta:
        ordering = ["name"]

    def __str__(self) -> str:
        return f"POS {self.name} ({self.key_prefix}…){'' if self.is_active else ' inactive'}"
