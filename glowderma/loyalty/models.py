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

from collections.abc import Iterable
from datetime import datetime
from typing import ClassVar, Self

from django.db import models
from django.db.models import Exists, F, OuterRef, Q, Subquery

from glowderma.core import enums, timeutils
from glowderma.core.labels import som_text
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

    version = models.PositiveIntegerField(unique=True, verbose_name="versiya")
    effective_from = models.DateTimeField(verbose_name="amal qilish boshlanishi")
    # before_purchase|with_purchase
    basis = models.CharField(max_length=16, choices=enums.RateBasis.choices, verbose_name="hisob asosi")
    earn_on_cashback_paid = models.BooleanField(verbose_name="cashback bilan to‘langan qismga ham cashback")
    # null = system / seed ("Tizim")
    author = models.ForeignKey(
        "accounts.StaffUser",
        null=True,
        on_delete=models.PROTECT,
        related_name="cashback_policies",
        verbose_name="muallif",
    )
    created_at = models.DateTimeField(default=timestamp_default, editable=False, verbose_name="yaratilgan")

    objects: ClassVar[CashbackPolicyManager[CashbackPolicy]] = CashbackPolicyManager()

    class Meta:
        verbose_name = "cashback siyosati"
        verbose_name_plural = "cashback siyosatlari"
        ordering = ["-version"]
        constraints = [
            models.CheckConstraint(
                condition=Q(effective_from__gt=F("created_at")), name="policy_effective_after_created"
            ),
        ]
        indexes = [models.Index(fields=["effective_from"], name="policy_effective_from_idx")]

    def __str__(self) -> str:
        basis = self.get_basis_display()
        return f"Cashback siyosati v{self.version} ({basis}, {self.effective_from:%Y-%m-%d} dan)"

    # Derived columns: ``with_derived()`` attaches them as annotations (Django assigns them through the
    # setters below); a plain instance loads both lazily with one query. They depend on *other* rows (a
    # later version), so ``refresh_from_db()`` drops the cache and the next read re-derives them.
    _DERIVED_CACHE_KEYS = ("_superseded_by", "_effective_to")

    def refresh_from_db(
        self,
        using: str | None = None,
        fields: Iterable[str] | None = None,
        from_queryset: models.QuerySet[Self] | None = None,
    ) -> None:
        for key in self._DERIVED_CACHE_KEYS:
            self.__dict__.pop(key, None)
        super().refresh_from_db(using=using, fields=fields, from_queryset=from_queryset)

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

    policy = models.ForeignKey(
        "loyalty.CashbackPolicy", on_delete=models.PROTECT, related_name="tiers", verbose_name="siyosat"
    )
    from_amount = models.BigIntegerField(verbose_name="summadan")  # inclusive, so'm
    # exclusive, so'm; null = open-ended
    to_amount = models.BigIntegerField(null=True, verbose_name="summagacha")
    percent = models.DecimalField(max_digits=5, decimal_places=2, verbose_name="foiz")
    created_at = models.DateTimeField(default=timestamp_default, editable=False, verbose_name="yaratilgan")

    class Meta:
        verbose_name = "cashback darajasi"
        verbose_name_plural = "cashback darajalari"
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
        "accounts.Customer", on_delete=models.PROTECT, related_name="loyalty_account", verbose_name="mijoz"
    )
    # signed; negative allowed (L5) and flagged in selectors
    balance = models.BigIntegerField(default=0, verbose_name="balans")

    class Meta:
        verbose_name = "cashback hisobi"
        verbose_name_plural = "cashback hisoblari"

    def __str__(self) -> str:
        return f"Mijoz #{self.customer_id} cashback hisobi: {som_text(self.balance)}"


class LedgerEntry(AppendOnlyModel):
    """One immutable, signed movement of a customer's cashback balance with the balance after it."""

    account = models.ForeignKey(
        "loyalty.LoyaltyAccount", on_delete=models.PROTECT, related_name="entries", verbose_name="hisob"
    )
    # credit|debit|adjustment|reversal
    kind = models.CharField(max_length=16, choices=enums.LedgerKind.choices, verbose_name="turi")
    amount = models.BigIntegerField(verbose_name="summa")  # signed so'm
    balance_after = models.BigIntegerField(verbose_name="keyingi balans")
    # order|billz_sale|admin
    source_type = models.CharField(
        max_length=16, choices=enums.LedgerSourceType.choices, verbose_name="manba turi"
    )
    order = models.ForeignKey(
        "commerce.Order",
        null=True,
        on_delete=models.PROTECT,
        related_name="ledger_entries",
        verbose_name="buyurtma",
    )
    purchase = models.ForeignKey(
        "loyalty.Purchase",
        null=True,
        on_delete=models.PROTECT,
        related_name="ledger_entries",
        verbose_name="xarid",
    )
    # order → str(order_id); billz_sale → Billz document no.
    source_ref = models.CharField(max_length=64, blank=True, verbose_name="manba havolasi")
    # credit-only snapshot of the engine inputs
    # eligible amount the percent was applied to
    base = models.BigIntegerField(null=True, verbose_name="asos")
    percent = models.DecimalField(max_digits=5, decimal_places=2, null=True, verbose_name="foiz")
    promo_excluded = models.BooleanField(
        default=False, verbose_name="aksiya mahsulotlari chiqarilgan"
    )  # promo items were excluded from the base
    policy = models.ForeignKey(
        "loyalty.CashbackPolicy",
        null=True,
        on_delete=models.PROTECT,
        related_name="ledger_entries",
        verbose_name="siyosat",
    )
    # reversal-only
    reverses = models.OneToOneField(
        "self",
        null=True,
        on_delete=models.PROTECT,
        related_name="reversed_by",
        verbose_name="bekor qilinadigan yozuv",
    )
    # adjustment-only
    reason = models.TextField(blank=True, verbose_name="sabab")
    actor = models.ForeignKey(
        "accounts.StaffUser",
        null=True,
        on_delete=models.PROTECT,
        related_name="ledger_adjustments",
        verbose_name="bajaruvchi",
    )
    # business time; API "at"
    at = models.DateTimeField(default=timestamp_default, editable=False, verbose_name="vaqt")

    class Meta:
        verbose_name = "cashback daftari yozuvi"
        verbose_name_plural = "cashback daftari"
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
            f"Yozuv #{self.pk} {self.get_kind_display()} {self.amount:+} → {self.balance_after} "
            f"(hisob #{self.account_id})"
        )


class CashbackHold(TimeStampedModel):
    """Cashback reserved at checkout for an unpaid order: captured into a debit on payment, else released."""

    class ReleaseReason(models.TextChoices):  # internal
        RESERVATION_EXPIRED = "reservation_expired", "Band muddati tugadi"
        ORDER_CANCELLED = "order_cancelled", "Buyurtma bekor qilindi"
        PAYMENT_FAILED = "payment_failed", "To‘lov amalga oshmadi"

    account = models.ForeignKey(
        "loyalty.LoyaltyAccount", on_delete=models.PROTECT, related_name="holds", verbose_name="hisob"
    )
    order = models.ForeignKey(
        "commerce.Order", on_delete=models.PROTECT, related_name="cashback_holds", verbose_name="buyurtma"
    )
    amount = models.BigIntegerField(verbose_name="summa")
    state = models.CharField(
        max_length=16,
        choices=enums.CashbackHoldState.choices,
        default=enums.CashbackHoldState.ACTIVE,
        verbose_name="holat",
    )
    # = Order.reservation_expires_at at creation
    expires_at = models.DateTimeField(verbose_name="amal qilish muddati")
    captured_entry = models.OneToOneField(
        "loyalty.LedgerEntry",
        null=True,
        on_delete=models.PROTECT,
        related_name="captured_hold",
        verbose_name="yechish yozuvi",
    )
    captured_at = models.DateTimeField(null=True, verbose_name="yechib olingan")
    released_at = models.DateTimeField(null=True, verbose_name="bo‘shatilgan")
    release_reason = models.CharField(
        max_length=24, blank=True, choices=ReleaseReason.choices, verbose_name="bo‘shatish sababi"
    )

    class Meta:
        verbose_name = "cashback bandi"
        verbose_name_plural = "cashback bandlari"
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
        return (
            f"Ushlab turish {som_text(self.amount)} · buyurtma №{self.order_id} ({self.get_state_display()})"
        )


# An online purchase whose Billz sale is not recorded: not pushed, or pushed without Billz's sale id
# (docs/integrations/billz.md §6). The condition of ``purchase_unpushed_online_idx``.
UNPUSHED_ONLINE = Q(source=enums.PurchaseSource.ONLINE, reversed=False) & (
    Q(billz_pushed_at__isnull=True) | Q(billz_sale_id="")
)


class Purchase(TimeStampedModel):
    """One purchase counting towards the lifetime total (paid online order or in-store Billz sale)."""

    customer = models.ForeignKey(
        "accounts.Customer", on_delete=models.PROTECT, related_name="purchases", verbose_name="mijoz"
    )
    # online|store_qr
    source = models.CharField(max_length=16, choices=enums.PurchaseSource.choices, verbose_name="manba")
    order = models.OneToOneField(
        "commerce.Order",
        null=True,
        on_delete=models.PROTECT,
        related_name="purchase",
        verbose_name="buyurtma",
    )
    # Billz identifiers
    billz_sale_id = models.CharField(
        max_length=64, blank=True, verbose_name="Billz sotuv ID"
    )  # Billz's own sale id (store, or online echo)
    billz_sale_ref = models.CharField(
        max_length=32, blank=True, verbose_name="Billz sotuv raqami"
    )  # human document number ("S-88213")
    # online: our correlation id, generated BEFORE the push (the outbox dedupe key, logs); Billz's own sale id
    # is billz_sale_id, stored the moment Billz created the sale (still a draft until billz_pushed_at is set)
    billz_order_uuid = models.UUIDField(null=True, verbose_name="Billz buyurtma UUID")
    billz_pushed_at = models.DateTimeField(
        null=True, verbose_name="Billz’ga yuborilgan"
    )  # online: set when billz.push_sale succeeded
    # online: a Billz payment request was sent at this instant and its outcome is not known yet (never paid
    # twice: the next attempt waits and re-reads the sale; cleared when Billz certainly did not take it)
    billz_payment_attempted_at = models.DateTimeField(null=True, verbose_name="Billz to‘lovi urinilgan")
    # engine inputs (snapshots)
    # gross total before cashback redemption (items_subtotal online)
    amount = models.BigIntegerField(verbose_name="summa")
    # part of amount that is promotional
    promo_amount = models.BigIntegerField(default=0, verbose_name="aksiya summasi")
    # part paid from the balance (= Order.cashback_used)
    cashback_paid = models.BigIntegerField(default=0, verbose_name="cashback bilan to‘langan")
    lifetime_before = models.BigIntegerField(
        verbose_name="oldingi umrbod xaridlar"
    )  # Σ non-reversed purchases of the customer before this one
    # engine result
    # version active at `at`; null = no policy existed
    policy = models.ForeignKey(
        "loyalty.CashbackPolicy",
        null=True,
        on_delete=models.PROTECT,
        related_name="purchases",
        verbose_name="siyosat",
    )
    # credited so'm (0 when no tier covers / eligible 0)
    cashback_amount = models.BigIntegerField(default=0, verbose_name="cashback summasi")
    credited_entry = models.OneToOneField(
        "loyalty.LedgerEntry",
        null=True,
        on_delete=models.PROTECT,
        related_name="credited_purchase",
        verbose_name="hisoblangan yozuv",
    )
    # reversal
    reversed = models.BooleanField(default=False, verbose_name="bekor qilingan")
    reversed_at = models.DateTimeField(null=True, verbose_name="bekor qilingan vaqt")
    # the reversal of credited_entry (null if nothing was credited)
    reversal_entry = models.OneToOneField(
        "loyalty.LedgerEntry",
        null=True,
        on_delete=models.PROTECT,
        related_name="reversed_purchase",
        verbose_name="bekor qilish yozuvi",
    )
    # business time: payment confirmation / Billz sale
    at = models.DateTimeField(default=timestamp_default, verbose_name="vaqt")

    class Meta:
        verbose_name = "xarid"
        verbose_name_plural = "xaridlar"
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
            # the online sales Billz has not recorded (consistency report K1, no look-back; loyalty.0005):
            # a handful of rows, so the check never walks the purchases of every order ever paid
            models.Index(fields=["order"], condition=UNPUSHED_ONLINE, name="purchase_unpushed_online_idx"),
        ]

    def __str__(self) -> str:
        ref = (
            f"buyurtma №{self.order_id}"
            if self.source == enums.PurchaseSource.ONLINE
            else f"Billz {self.billz_sale_ref or self.billz_sale_id}"
        )
        suffix = " · bekor qilingan" if self.reversed else ""
        return f"Xarid #{self.pk} {self.get_source_display()} {som_text(self.amount)} ({ref}){suffix}"


class PurchaseItem(models.Model):
    """Line snapshot of a purchase (what was bought, at which unit price, promo or not)."""

    purchase = models.ForeignKey(
        "loyalty.Purchase", on_delete=models.CASCADE, related_name="items", verbose_name="xarid"
    )
    position = models.PositiveSmallIntegerField(verbose_name="tartib")
    # null for Billz lines not in the catalog
    product = models.ForeignKey(
        "catalog.Product",
        null=True,
        on_delete=models.PROTECT,
        related_name="purchase_items",
        verbose_name="mahsulot",
    )
    billz_product_id = models.CharField(max_length=32, blank=True, verbose_name="Billz mahsulot ID")
    # uz display name at purchase time, or the Billz name
    name = models.CharField(max_length=200, verbose_name="nomi")
    quantity = models.PositiveIntegerField(verbose_name="miqdor")
    unit_price = models.BigIntegerField(verbose_name="birlik narxi")
    promo = models.BooleanField(default=False, verbose_name="aksiya")
    line_total = models.BigIntegerField(verbose_name="qator summasi")  # quantity × unit_price

    class Meta:
        verbose_name = "xarid qatori"
        verbose_name_plural = "xarid qatorlari"
        ordering = ["purchase_id", "position"]
        constraints = [
            models.UniqueConstraint(fields=["purchase", "position"], name="purchaseitem_position_unique"),
            models.CheckConstraint(
                condition=Q(quantity__gt=0, unit_price__gte=0, line_total__gte=0), name="purchaseitem_amounts"
            ),
        ]

    def __str__(self) -> str:
        return f"{self.name} × {self.quantity} @ {som_text(self.unit_price)} (xarid #{self.purchase_id})"


class PosDevice(TimeStampedModel):
    """A cashier terminal allowed to call ``POST /api/v1/pos/cashback/resolve-qr`` (M2); key stored hashed."""

    name = models.CharField(max_length=100, verbose_name="nomi")
    # free text; no FK to professionals
    location_label = models.CharField(max_length=100, blank=True, verbose_name="joy nomi")
    # first 8–12 chars of the key; names the device
    key_prefix = models.CharField(max_length=12, unique=True, verbose_name="kalit prefiksi")
    key_hash = models.CharField(
        max_length=64, unique=True, verbose_name="kalit xeshi"
    )  # SHA-256 hex of the full key
    is_active = models.BooleanField(default=True, verbose_name="faol")
    last_used_at = models.DateTimeField(null=True, verbose_name="oxirgi foydalanish")
    created_by = models.ForeignKey(
        "accounts.StaffUser",
        null=True,
        on_delete=models.PROTECT,
        related_name="pos_devices",
        verbose_name="yaratgan",
    )

    class Meta:
        verbose_name = "POS qurilma"
        verbose_name_plural = "POS qurilmalar"
        ordering = ["name"]

    def __str__(self) -> str:
        return f"POS {self.name} ({self.key_prefix}…){'' if self.is_active else ' · nofaol'}"
