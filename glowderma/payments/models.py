"""Models of `payments` — docs/plans/m1-models/02-… §2 (slice B2).

``Payment`` is one attempt to collect an order's ``total`` with one provider (at most one ``pending``/``paid``
row per order); ``Refund`` is a request to return a paid amount, driven by the contract's six-state machine
(at most one open refund per payment). Every state change happens inside ``payments.services`` (B5) under
the Payment lock with the Order already locked by the orchestrator; the Django admin is read-only. Raw
provider callbacks live in ``integrations.WebhookInbox`` (P-1): ``provider_state`` holds only the structured
protocol fields the design lists.
"""

from django.db import models
from django.db.models import Q

from glowderma.core import enums
from glowderma.core.labels import som_text
from glowderma.core.models import TimeStampedModel, timestamp_default

# Payment statuses that make a payment "active" for the one-active-per-order partial unique.
ACTIVE_PAYMENT_STATUSES: tuple[str, ...] = (enums.PaymentStatus.PENDING, enums.PaymentStatus.PAID)
# ``Payment.error_code`` of a refused capture (customer contract §2.13.0): money a provider took for a
# payment we
# did not honour; the row is ``paid`` until its refund succeeds and never counts as the order's active
# payment.
LATE_CAPTURE = "late_capture"
# Payment statuses that require a provider transaction id (the money moved under that id).
SETTLED_PAYMENT_STATUSES: tuple[str, ...] = (enums.PaymentStatus.PAID, enums.PaymentStatus.REFUNDED)
# Refund states that keep a refund "open" for the one-open-per-payment partial unique.
OPEN_REFUND_STATES: tuple[str, ...] = (
    enums.RefundState.REQUESTED,
    enums.RefundState.SUBMITTING,
    enums.RefundState.MANUAL_REQUIRED,
    enums.RefundState.UNKNOWN,
)
# Refund states the dashboard's ``attention.refunds_needing_action`` counts (the partial index).
ATTENTION_REFUND_STATES: tuple[str, ...] = (
    enums.RefundState.FAILED,
    enums.RefundState.UNKNOWN,
    enums.RefundState.MANUAL_REQUIRED,
)
# Refund states that imply a handover to the provider happened (``submitted_at`` set).
SUBMITTED_REFUND_STATES: tuple[str, ...] = (enums.RefundState.SUBMITTING, enums.RefundState.UNKNOWN)


class Payment(TimeStampedModel):
    """One attempt to collect an order's ``total`` with one provider; ``amount`` never changes."""

    order = models.ForeignKey(
        "commerce.Order", on_delete=models.PROTECT, related_name="payments", verbose_name="buyurtma"
    )
    # click|payme
    provider = models.CharField(
        max_length=16, choices=enums.PaymentProvider.choices, verbose_name="provayder"
    )
    # pending|paid|failed|refunded
    status = models.CharField(
        max_length=16,
        choices=enums.PaymentStatus.choices,
        default=enums.PaymentStatus.PENDING,
        verbose_name="holat",
    )
    amount = models.BigIntegerField(verbose_name="summa")  # = Order.total at creation
    # Click click_trans_id / Payme transaction id; "" → null in the API
    provider_txn_id = models.CharField(
        max_length=64, blank=True, db_index=True, verbose_name="provayder tranzaksiya ID"
    )
    confirmed_at = models.DateTimeField(null=True, verbose_name="tasdiqlangan")
    failed_at = models.DateTimeField(null=True, verbose_name="xato vaqti")
    refunded_at = models.DateTimeField(null=True, verbose_name="qaytarilgan")
    # provider error code as text ("-9", "-31008", "cancelled", "abandoned", "expired", "late_capture")
    error_code = models.CharField(max_length=32, blank=True, verbose_name="xato kodi")
    error_message = models.CharField(max_length=254, blank=True, verbose_name="xato matni")
    # provider protocol state, keys in design 02 §2.1 (never a raw payload: WebhookInbox holds those)
    provider_state = models.JSONField(default=dict, blank=True)

    class Meta:
        verbose_name = "to‘lov"
        verbose_name_plural = "to‘lovlar"
        ordering = ["-created_at", "-id"]
        constraints = [
            models.UniqueConstraint(
                fields=["order"],
                condition=Q(status__in=list(ACTIVE_PAYMENT_STATUSES)) & ~Q(error_code=LATE_CAPTURE),
                name="payment_one_active_per_order",
            ),
            models.UniqueConstraint(
                fields=["provider", "provider_txn_id"],
                condition=~Q(provider_txn_id=""),
                name="payment_provider_txn_unique",
            ),
            models.CheckConstraint(condition=Q(amount__gt=0), name="payment_amount_positive"),
            models.CheckConstraint(
                condition=(
                    Q(
                        status=enums.PaymentStatus.PENDING,
                        confirmed_at__isnull=True,
                        failed_at__isnull=True,
                        refunded_at__isnull=True,
                    )
                    | Q(
                        status=enums.PaymentStatus.PAID,
                        confirmed_at__isnull=False,
                        failed_at__isnull=True,
                        refunded_at__isnull=True,
                    )
                    | Q(
                        status=enums.PaymentStatus.FAILED,
                        failed_at__isnull=False,
                        confirmed_at__isnull=True,
                        refunded_at__isnull=True,
                    )
                    | Q(
                        status=enums.PaymentStatus.REFUNDED,
                        confirmed_at__isnull=False,
                        refunded_at__isnull=False,
                        failed_at__isnull=True,
                    )
                ),
                name="payment_status_times",
            ),
            models.CheckConstraint(
                condition=~Q(status__in=list(SETTLED_PAYMENT_STATUSES)) | ~Q(provider_txn_id=""),
                name="payment_paid_has_txn",
            ),
        ]
        indexes = [
            models.Index(fields=["order", "id"], name="payment_order_idx"),
            models.Index(fields=["created_at", "id"], name="payment_created_idx"),
            models.Index(fields=["status", "created_at"], name="payment_status_created_idx"),
            models.Index(
                fields=["confirmed_at"],
                condition=Q(status__in=list(SETTLED_PAYMENT_STATUSES)),
                name="payment_confirmed_idx",
            ),
            # late captures whose money is not back yet (consistency report K5; payments.0003)
            models.Index(
                fields=["confirmed_at"],
                condition=Q(error_code=LATE_CAPTURE, status=enums.PaymentStatus.PAID),
                name="payment_late_capture_open_idx",
            ),
        ]

    def __str__(self) -> str:
        return (
            f"To‘lov #{self.pk} {self.get_provider_display()} · {self.get_status_display()} · "
            f"{som_text(self.amount)} (buyurtma №{self.order_id})"
        )


class Refund(TimeStampedModel):
    """A request to return a paid amount to the customer (M1 refunds are full: ``amount == payment.amount``).

    State machine (design 02 §2.2; every transition is a ``payments.services`` call audited
    ``refund.state_changed``):

    * ``— → requested`` (Click: row + outbox ``refund.submit``) · ``— → manual_required`` (Payme cabinet)
    * ``requested → submitting`` (handler claims the row: ``submitted_at`` first time, ``attempts += 1``)
    * ``requested → failed`` (the ``refund.submit`` message dies before any submission:
      ``error_code = "dead_letter"``)
    * ``submitting → succeeded`` (``provider_refund_id``, ``completed_at``; the payment turns ``refunded``
      when the succeeded sum equals ``payment.amount``)
    * ``submitting → failed`` (definitive provider error)
    * ``submitting → unknown`` (transport failure after submission and the status check was inconclusive;
      never auto-retried)
    * ``submitting → submitting`` (outbox redelivery: status check first)
    * ``manual_required → succeeded`` (Payme ``CancelTransaction`` of a performed transaction)
    * ``unknown → succeeded | failed`` (M3 reconciliation / staff resolve)
    * ``failed`` and ``succeeded`` are terminal; a retry is a **new** row with ``origin = manual`` (the
      partial unique allows it because ``failed`` is closed).
    """

    class Origin(models.TextChoices):  # internal
        ORDER_CANCELLED = "order_cancelled", "Buyurtma bekor qilindi"
        LATE_CAPTURE = "late_capture", "Kech kelgan to‘lov"  # capture landed after expiry (M2 auto-refund)
        MANUAL = "manual", "Qo‘lda"  # staff-initiated retry (M3 command)

    payment = models.ForeignKey(
        "payments.Payment", on_delete=models.PROTECT, related_name="refunds", verbose_name="to‘lov"
    )
    # requested|submitting|succeeded|failed|manual_required|unknown
    state = models.CharField(
        max_length=16,
        choices=enums.RefundState.choices,
        default=enums.RefundState.REQUESTED,
        verbose_name="holat",
    )
    # = payment.amount in M1 (full refunds only; service invariant)
    amount = models.BigIntegerField(verbose_name="summa")
    origin = models.CharField(
        max_length=16, choices=Origin.choices, default=Origin.ORDER_CANCELLED, verbose_name="manba"
    )
    provider_refund_id = models.CharField(max_length=64, blank=True, verbose_name="provayder qaytarish ID")
    # null = system
    requested_by = models.ForeignKey(
        "accounts.StaffUser",
        null=True,
        on_delete=models.PROTECT,
        related_name="refunds_requested",
        verbose_name="so‘ragan",
    )
    requested_at = models.DateTimeField(default=timestamp_default, verbose_name="so‘ralgan")
    # first handover to the provider
    submitted_at = models.DateTimeField(null=True, verbose_name="yuborilgan")
    last_attempt_at = models.DateTimeField(null=True)
    completed_at = models.DateTimeField(null=True, verbose_name="yakunlangan")  # succeeded only
    attempts = models.PositiveIntegerField(default=0, verbose_name="urinishlar")
    error_code = models.CharField(max_length=32, blank=True, verbose_name="xato kodi")
    last_error = models.TextField(blank=True, verbose_name="oxirgi xato")
    # click: {payment_status, error, error_note, checked_at}; payme: {cancel_time, reason, state: -2}
    provider_state = models.JSONField(default=dict, blank=True)
    # why the refund is (or was) manual_required: payme | no_paydoc | fake_mode; "" = never manual (M3)
    manual_reason = models.CharField(
        max_length=16,
        choices=enums.RefundManualReason.choices,
        blank=True,
        verbose_name="qo‘lda kiritish sababi",
    )

    class Meta:
        verbose_name = "qaytarish"
        verbose_name_plural = "qaytarishlar"
        ordering = ["-requested_at", "-id"]
        constraints = [
            models.UniqueConstraint(
                fields=["payment"],
                condition=Q(state__in=list(OPEN_REFUND_STATES)),
                name="refund_one_open_per_payment",
            ),
            models.CheckConstraint(condition=Q(amount__gt=0), name="refund_amount_positive"),
            models.CheckConstraint(
                condition=(
                    Q(state=enums.RefundState.SUCCEEDED, completed_at__isnull=False)
                    | (~Q(state=enums.RefundState.SUCCEEDED) & Q(completed_at__isnull=True))
                ),
                name="refund_completed_at",
            ),
            models.CheckConstraint(
                condition=~Q(state__in=list(SUBMITTED_REFUND_STATES)) | Q(submitted_at__isnull=False),
                name="refund_submitted_at",
            ),
        ]
        indexes = [
            models.Index(fields=["payment", "id"], name="refund_payment_idx"),
            models.Index(
                fields=["state"],
                condition=Q(state__in=list(ATTENTION_REFUND_STATES)),
                name="refund_attention_idx",
            ),
        ]

    def __str__(self) -> str:
        return (
            f"Qaytarish #{self.pk} · {self.get_state_display()} · {som_text(self.amount)} "
            f"(to‘lov #{self.payment_id})"
        )
