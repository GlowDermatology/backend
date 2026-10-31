"""Models of `notifications` — docs/plans/m1-models/03-… §3 (slice B2).

``StatusTemplate`` (four rows from ``0002_seed_status_templates``, re-ensured by ``post_migrate``),
``CustomerSegment`` + ``SegmentMember`` (rule-based audiences with manual members), ``Campaign`` +
``CampaignRecipient`` (admin pushes and their per-device outcome) and ``InboxNotification`` (the customer's
in-app feed, de-duplicated by ``dedupe_key``).
"""

from django.core.serializers.json import DjangoJSONEncoder
from django.db import models
from django.db.models import Q
from django.db.models.functions import Coalesce, Lower

from glowderma.core import enums
from glowderma.core.models import TimeStampedModel, timestamp_default
from glowderma.notifications import media


class StatusTemplate(models.Model):
    """The push text for one of the four order statuses that trigger a push; ``{raqam}`` is the only
    placeholder."""

    status = models.CharField(
        max_length=24, choices=enums.TemplateStatus.choices, unique=True, verbose_name="holat"
    )
    text_uz = models.CharField(max_length=180, verbose_name="matn (uz)")
    text_ru = models.CharField(max_length=180, blank=True, verbose_name="matn (ru)")
    text_en = models.CharField(max_length=180, blank=True, verbose_name="matn (en)")
    created_at = models.DateTimeField(default=timestamp_default, editable=False, verbose_name="yaratilgan")
    updated_at = models.DateTimeField(default=timestamp_default, verbose_name="yangilangan")
    updated_by = models.ForeignKey(
        "accounts.StaffUser", null=True, on_delete=models.SET_NULL, related_name="+"
    )

    class Meta:
        verbose_name = "holat shabloni"
        verbose_name_plural = "holat shablonlari"
        ordering = ["id"]
        constraints = [
            models.CheckConstraint(condition=Q(text_uz__regex=r"^.{5,180}$"), name="template_text_uz_5_180"),
            models.CheckConstraint(
                condition=~Q(text_uz__regex=r"\{(?!raqam\})")
                & ~Q(text_ru__regex=r"\{(?!raqam\})")
                & ~Q(text_en__regex=r"\{(?!raqam\})"),
                name="template_only_raqam_placeholder",
            ),
        ]

    def __str__(self) -> str:
        return f"Template {self.status}: {self.text_uz[:40]}"


class CustomerSegment(TimeStampedModel):
    """A named audience: rule-based (``min_online_orders``, ``min_lifetime_amount``, ANDed when set) plus
    manual members."""

    name = models.CharField(max_length=60, verbose_name="nomi")
    # null = rule off
    min_online_orders = models.PositiveIntegerField(null=True, verbose_name="min. onlayn buyurtmalar")
    # so'm; null = rule off
    min_lifetime_amount = models.BigIntegerField(null=True, verbose_name="min. umumiy xarid")
    created_by = models.ForeignKey(
        "accounts.StaffUser", null=True, on_delete=models.SET_NULL, related_name="+", verbose_name="yaratgan"
    )

    class Meta:
        verbose_name = "segment"
        verbose_name_plural = "segmentlar"
        ordering = ["id"]
        constraints = [
            models.UniqueConstraint(Lower("name"), name="segment_name_ci_unique"),
            models.CheckConstraint(condition=~Q(name=""), name="segment_name_required"),
            models.CheckConstraint(
                condition=Q(min_online_orders__isnull=True) | Q(min_online_orders__gte=1),
                name="segment_min_orders_positive",
            ),
            models.CheckConstraint(
                condition=Q(min_lifetime_amount__isnull=True) | Q(min_lifetime_amount__gte=1),
                name="segment_min_amount_positive",
            ),
        ]

    def __str__(self) -> str:
        return self.name


class SegmentMember(models.Model):
    """A manual membership of one customer in one segment."""

    segment = models.ForeignKey(
        "notifications.CustomerSegment",
        on_delete=models.CASCADE,
        related_name="manual_members",
        verbose_name="segment",
    )
    customer = models.ForeignKey(
        "accounts.Customer",
        on_delete=models.CASCADE,
        related_name="segment_memberships",
        verbose_name="mijoz",
    )
    added_by = models.ForeignKey(
        "accounts.StaffUser", null=True, on_delete=models.SET_NULL, related_name="+", verbose_name="qo‘shgan"
    )
    added_at = models.DateTimeField(default=timestamp_default, editable=False, verbose_name="qo‘shilgan")

    class Meta:
        verbose_name = "segment a’zosi"
        verbose_name_plural = "segment a’zolari"
        ordering = ["segment_id", "added_at", "id"]
        constraints = [models.UniqueConstraint(fields=["segment", "customer"], name="segmentmember_unique")]
        indexes = [models.Index(fields=["customer"], name="segmentmember_customer_idx")]

    def __str__(self) -> str:
        return f"Customer {self.customer_id} in segment {self.segment_id}"


class Campaign(TimeStampedModel):
    """One admin-authored push (promo or general) to an audience, with its schedule, send progress and
    result counters; the contract's ``NotificationRecord``."""

    class State(models.TextChoices):  # internal; API renders sending as "queued"
        SCHEDULED = "scheduled"
        QUEUED = "queued"
        SENDING = "sending"
        SENT = "sent"
        CANCELLED = "cancelled"  # M3: a scheduled campaign staff cancelled before its send started

    # promo|general
    kind = models.CharField(max_length=8, choices=enums.NotificationKind.choices, verbose_name="turi")
    title = models.CharField(max_length=60, verbose_name="sarlavha")
    body = models.CharField(max_length=180, verbose_name="matn")
    # MEDIA_ROOT/campaigns/<id>/<hash>.<ext>
    image = models.ImageField(
        upload_to=media.campaign_image_path, blank=True, max_length=255, verbose_name="rasm"
    )
    # none|product|url
    link_kind = models.CharField(
        max_length=8, choices=enums.LinkKind.choices, default=enums.LinkKind.NONE, verbose_name="havola turi"
    )
    link_product = models.ForeignKey(
        "catalog.Product",
        null=True,
        on_delete=models.SET_NULL,
        related_name="+",
        verbose_name="havola mahsuloti",
    )
    link_product_label = models.CharField(max_length=120, blank=True)  # uz display name at creation
    # gap ADM-10.1: the https link the app opens (kind "url" only; "" otherwise; decision GC-3)
    link_url = models.CharField(max_length=500, blank=True, default="", verbose_name="havola URL")
    # all|segment|customer
    audience_type = models.CharField(
        max_length=8, choices=enums.AudienceType.choices, verbose_name="auditoriya turi"
    )
    audience_segment = models.ForeignKey(
        "notifications.CustomerSegment", null=True, on_delete=models.SET_NULL, related_name="campaigns"
    )
    audience_segment_name = models.CharField(max_length=60, blank=True)  # snapshot
    audience_customer = models.ForeignKey(
        "accounts.Customer", null=True, on_delete=models.PROTECT, related_name="+"
    )
    audience_phone = models.CharField(max_length=13, blank=True)  # snapshot
    # pushable devices at creation; recomputed at send
    recipient_count = models.PositiveIntegerField(default=0, verbose_name="qabul qiluvchilar soni")
    state = models.CharField(max_length=16, choices=State.choices, default=State.QUEUED, verbose_name="holat")
    scheduled_at = models.DateTimeField(null=True, verbose_name="rejalashtirilgan")
    started_at = models.DateTimeField(null=True, verbose_name="boshlangan")
    sent_at = models.DateTimeField(null=True, verbose_name="yuborilgan")
    # M3: set with state cancelled only
    cancelled_at = models.DateTimeField(null=True, verbose_name="bekor qilingan")
    sent = models.PositiveIntegerField(null=True, verbose_name="yuborildi")  # FCM accepted
    failed = models.PositiveIntegerField(null=True, verbose_name="xato")  # FCM rejected / no token
    # M2 acknowledgements; null in M1
    delivered = models.PositiveIntegerField(null=True, verbose_name="yetkazildi")
    send_cursor = models.BigIntegerField(default=0)  # last CampaignRecipient.id handed to the adapter
    batches_dispatched = models.PositiveIntegerField(default=0)
    last_error = models.TextField(blank=True, verbose_name="oxirgi xato")
    author = models.ForeignKey(
        "accounts.StaffUser", null=True, on_delete=models.PROTECT, related_name="campaigns"
    )

    class Meta:
        verbose_name = "kampaniya"
        verbose_name_plural = "kampaniyalar"
        ordering = [Coalesce("scheduled_at", "created_at").desc(), "-id"]
        constraints = [
            models.CheckConstraint(
                condition=Q(title__regex=r"^.{3,60}$") & Q(body__regex=r"^.{5,180}$"),
                name="campaign_text_lengths",
            ),
            models.CheckConstraint(
                condition=(
                    Q(link_kind="none", link_product__isnull=True, link_product_label="", link_url="")
                    | Q(link_kind="product", link_url="") & ~Q(link_product_label="")
                    | Q(
                        link_kind="url",
                        link_product__isnull=True,
                        link_product_label="",
                        link_url__startswith="https://",
                    )
                ),
                name="campaign_link_fields",
            ),
            models.CheckConstraint(
                condition=(
                    Q(
                        audience_type="all",
                        audience_segment__isnull=True,
                        audience_segment_name="",
                        audience_customer__isnull=True,
                        audience_phone="",
                    )
                    | Q(audience_type="segment", audience_customer__isnull=True, audience_phone="")
                    & ~Q(audience_segment_name="")
                    | Q(
                        audience_type="customer",
                        audience_customer__isnull=False,
                        audience_segment__isnull=True,
                        audience_segment_name="",
                    )
                    & Q(audience_phone__regex=r"^\+998\d{9}$")
                ),
                name="campaign_audience_fields",
            ),
            models.CheckConstraint(
                condition=(
                    Q(
                        state="scheduled",
                        scheduled_at__isnull=False,
                        started_at__isnull=True,
                        sent_at__isnull=True,
                        sent__isnull=True,
                        failed__isnull=True,
                    )
                    | Q(
                        state="queued",
                        started_at__isnull=True,
                        sent_at__isnull=True,
                        sent__isnull=True,
                        failed__isnull=True,
                    )
                    | Q(
                        state="sending",
                        started_at__isnull=False,
                        sent_at__isnull=True,
                        sent__isnull=True,
                        failed__isnull=True,
                    )
                    | Q(
                        state="sent",
                        started_at__isnull=False,
                        sent_at__isnull=False,
                        sent__isnull=False,
                        failed__isnull=False,
                    )
                    | Q(
                        state="cancelled",
                        scheduled_at__isnull=False,
                        started_at__isnull=True,
                        sent_at__isnull=True,
                        sent__isnull=True,
                        failed__isnull=True,
                    )
                ),
                name="campaign_state_fields",
            ),
            models.CheckConstraint(
                condition=Q(state="cancelled", cancelled_at__isnull=False)
                | ~Q(state="cancelled") & Q(cancelled_at__isnull=True),
                name="campaign_cancelled_at",
            ),
        ]
        indexes = [
            models.Index(
                Coalesce("scheduled_at", "created_at").desc(), models.F("id").desc(), name="campaign_at_idx"
            ),
            models.Index(fields=["scheduled_at"], condition=Q(state="scheduled"), name="campaign_due_idx"),
            models.Index(fields=["state", "kind"], name="campaign_state_kind_idx"),
            models.Index(
                fields=["audience_segment"], condition=Q(state="scheduled"), name="campaign_sched_segment_idx"
            ),
        ]

    def __str__(self) -> str:
        return f"Campaign #{self.pk} [{self.kind}] {self.title} ({self.state})"


class CampaignRecipient(models.Model):
    """One target device of a campaign with its send outcome; the unit of idempotency for batched sends."""

    class State(models.TextChoices):  # internal
        PENDING = "pending"
        SENT = "sent"
        FAILED = "failed"
        SKIPPED = "skipped"  # device gone / push disabled between materialise and send

    campaign = models.ForeignKey(
        "notifications.Campaign",
        on_delete=models.CASCADE,
        related_name="recipients",
        verbose_name="kampaniya",
    )
    customer = models.ForeignKey(
        "accounts.Customer", on_delete=models.CASCADE, related_name="+", verbose_name="mijoz"
    )
    device = models.ForeignKey(
        "accounts.Device", null=True, on_delete=models.SET_NULL, related_name="+", verbose_name="qurilma"
    )
    state = models.CharField(max_length=8, choices=State.choices, default=State.PENDING, verbose_name="holat")
    error = models.CharField(max_length=254, blank=True, verbose_name="xato")
    provider_message_id = models.CharField(max_length=128, blank=True)  # FCM message id
    # FCM error / batch
    provider_state = models.JSONField(default=dict, blank=True, encoder=DjangoJSONEncoder)
    sent_at = models.DateTimeField(null=True, verbose_name="yuborilgan")
    delivered_at = models.DateTimeField(null=True, verbose_name="yetkazilgan")  # M2 acknowledgement
    inbox = models.OneToOneField(
        "notifications.InboxNotification",
        null=True,
        on_delete=models.SET_NULL,
        related_name="campaign_recipient",
        verbose_name="inbox xabari",
    )
    created_at = models.DateTimeField(default=timestamp_default, editable=False, verbose_name="yaratilgan")

    class Meta:
        verbose_name = "qabul qiluvchi"
        verbose_name_plural = "qabul qiluvchilar"
        ordering = ["campaign_id", "id"]
        constraints = [
            models.UniqueConstraint(
                fields=["campaign", "customer"], name="recipient_campaign_customer_unique"
            ),
            models.UniqueConstraint(
                fields=["campaign", "device"],
                condition=Q(device__isnull=False),
                name="recipient_campaign_device_unique",
            ),
            models.CheckConstraint(
                condition=Q(state="sent", sent_at__isnull=False)
                | (~Q(state="sent") & Q(sent_at__isnull=True)),
                name="recipient_sent_at",
            ),
        ]
        indexes = [
            models.Index(fields=["campaign", "state"], name="recipient_campaign_state_idx"),
            models.Index(fields=["customer", "-created_at"], name="recipient_customer_idx"),
        ]

    def __str__(self) -> str:
        return f"Campaign {self.campaign_id} → customer {self.customer_id} ({self.state})"


class InboxNotification(TimeStampedModel):
    """The customer's in-app feed: one row per message actually addressed to the customer, de-duplicated by
    ``dedupe_key`` so at-least-once handlers never double-notify."""

    class Kind(models.TextChoices):  # internal
        PROMO = "promo"
        GENERAL = "general"
        ORDER_STATUS = "order_status"
        APPOINTMENT = "appointment"

    class SourceType(models.TextChoices):  # internal
        CAMPAIGN = "campaign"
        ORDER = "order"
        APPOINTMENT = "appointment"
        # M2 account_security notices (customer contract §2.12): source_id = the customer id.
        ACCOUNT = "account"

    class PushState(models.TextChoices):  # internal
        NO_DEVICE = "no_device"
        QUEUED = "queued"
        SENDING = "sending"  # claimed before the FCM call (crash → never resent)
        SENT = "sent"
        FAILED = "failed"

    customer = models.ForeignKey(
        "accounts.Customer", on_delete=models.CASCADE, related_name="inbox", verbose_name="mijoz"
    )
    kind = models.CharField(max_length=16, choices=Kind.choices, verbose_name="turi")
    title = models.CharField(max_length=80, verbose_name="sarlavha")
    body = models.CharField(max_length=300, verbose_name="matn")
    # {"kind":"none"} | {"kind":"product","product_id":4} | {"kind":"order","order_id":10248} |
    link = models.JSONField(default=dict, encoder=DjangoJSONEncoder, verbose_name="havola")
    # {"kind":"url","url":"https://…"} | …
    image_url = models.CharField(max_length=500, blank=True)
    source_type = models.CharField(max_length=16, choices=SourceType.choices, verbose_name="manba turi")
    source_id = models.BigIntegerField()  # campaign / order / appointment / customer (account) id
    # order_status only
    template_status = models.CharField(
        max_length=24, blank=True, choices=enums.TemplateStatus.choices, verbose_name="shablon holati"
    )
    # rendering language
    language = models.CharField(max_length=2, choices=enums.ContentLanguage.choices, verbose_name="til")
    dedupe_key = models.CharField(max_length=120, unique=True, verbose_name="takrorlanmaslik kaliti")
    push_state = models.CharField(
        max_length=16, choices=PushState.choices, default=PushState.NO_DEVICE, verbose_name="push holati"
    )
    pushed_at = models.DateTimeField(null=True, verbose_name="push yuborilgan")
    push_error = models.CharField(max_length=254, blank=True, verbose_name="push xatosi")
    read_at = models.DateTimeField(null=True, verbose_name="o‘qilgan")
    delivered_at = models.DateTimeField(null=True, verbose_name="yetkazilgan")  # M2 acknowledgement

    class Meta:
        verbose_name = "inbox xabari"
        verbose_name_plural = "inbox xabarlari"
        ordering = ["-created_at", "-id"]
        constraints = [
            models.CheckConstraint(
                condition=(
                    Q(kind="order_status", source_type="order") & ~Q(template_status="")
                    | Q(kind="appointment", source_type="appointment", template_status="")
                    | Q(kind__in=["promo", "general"], source_type="campaign", template_status="")
                    | Q(kind="general", source_type="account", template_status="")
                ),
                name="inbox_kind_source_fields",
            ),
            models.CheckConstraint(condition=~Q(title="") & ~Q(body=""), name="inbox_text_required"),
        ]
        indexes = [
            models.Index(fields=["customer", "-created_at", "-id"], name="inbox_customer_created_idx"),
            models.Index(
                fields=["customer"], condition=Q(read_at__isnull=True), name="inbox_customer_unread_idx"
            ),
            models.Index(fields=["source_type", "source_id"], name="inbox_source_idx"),
        ]

    def __str__(self) -> str:
        return f"Inbox {self.kind} → customer {self.customer_id}: {self.title[:40]}"
