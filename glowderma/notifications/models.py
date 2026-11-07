"""Models of `notifications` — docs/plans/m1-models/03-… §3 (slice B2).

``StatusTemplate`` (four rows from ``0002_seed_status_templates``, re-ensured by ``post_migrate``),
``CustomerSegment`` + ``SegmentMember`` (rule-based audiences with manual members), ``Campaign`` +
``CampaignRecipient`` (admin pushes and their per-device outcome), ``TestDevice`` + ``CampaignTest`` (the
approval workflow's test pushes to real staff devices) and ``InboxNotification`` (the customer's in-app feed,
de-duplicated by ``dedupe_key``).
"""

from django.core.serializers.json import DjangoJSONEncoder
from django.db import models
from django.db.models import F, Q
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
        "accounts.StaffUser",
        null=True,
        on_delete=models.SET_NULL,
        related_name="+",
        verbose_name="yangilagan",
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
        return f"Shablon «{self.get_status_display()}»: {self.text_uz[:40]}"


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
        return f"Mijoz #{self.customer_id} · segment #{self.segment_id}"


# Field groups of ``campaign_state_fields`` (legacy rows: an old ``cancelled`` row carries ``scheduled_at``
# and no approval fields, an old ``sent`` row no approval fields either — both still pass).
_NOT_STARTED = Q(
    started_at__isnull=True,
    sent_at__isnull=True,
    sent__isnull=True,
    failed__isnull=True,
    failed_at__isnull=True,
)
_NOT_APPROVED = Q(approved_at__isnull=True, approved_revision__isnull=True, dispatch_after__isnull=True)
_NOT_STOPPED = Q(stop_requested_at__isnull=True, stopped_at__isnull=True)


class Campaign(TimeStampedModel):
    """One admin-authored push (promo or general) to an audience, with its approval trail, schedule, send
    progress and result counters; the contract's ``NotificationRecord``.

    Approval workflow (docs/api/admin-api-v1.md §2.8): staff write a ``draft``; a superuser test-pushes it
    to a registered :class:`TestDevice`, confirms the test (``tested``), approves it (``scheduled`` — nothing
    is dispatched before ``dispatch_after``) and may cancel it, return it to draft or, once ``sending``, stop
    it (``stopping`` → ``partially_sent`` / ``cancelled``). Approvals and tests are bound to
    ``content_revision``.
    """

    class State(models.TextChoices):  # the API literals (core.enums.NotificationState)
        DRAFT = "draft", "Qoralama"
        TESTED = "tested", "Test tasdiqlangan"
        SCHEDULED = "scheduled", "Rejalashtirilgan"  # approved; waits for ``dispatch_after``
        SENDING = "sending", "Yuborilmoqda"
        STOPPING = "stopping", "To‘xtatilmoqda"  # emergency stop requested; the next batch finalises it
        SENT = "sent", "Yuborildi"
        PARTIALLY_SENT = "partially_sent", "Qisman yuborildi"  # stopped / failed after some pushes went out
        CANCELLED = "cancelled", "Bekor qilindi"  # cancelled before the start, or stopped before any push
        FAILED = "failed", "Yuborilmadi"  # a dead ``campaign.send_batch`` before any push went out

    # The states nothing changes any more.
    TERMINAL_STATES = frozenset({"sent", "partially_sent", "cancelled", "failed"})
    # The states an author may still edit or delete.
    EDITABLE_STATES = frozenset({"draft", "tested"})

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
    link_product_label = models.CharField(
        max_length=120, blank=True, verbose_name="havola mahsuloti nomi"
    )  # uz display name at creation
    # gap ADM-10.1: the https link the app opens (kind "url" only; "" otherwise; decision GC-3)
    link_url = models.CharField(max_length=500, blank=True, default="", verbose_name="havola URL")
    # all|segment|customer
    audience_type = models.CharField(
        max_length=8, choices=enums.AudienceType.choices, verbose_name="auditoriya turi"
    )
    audience_segment = models.ForeignKey(
        "notifications.CustomerSegment",
        null=True,
        on_delete=models.SET_NULL,
        related_name="campaigns",
        verbose_name="auditoriya segmenti",
    )
    audience_segment_name = models.CharField(
        max_length=60, blank=True, verbose_name="segment nomi"
    )  # snapshot
    audience_customer = models.ForeignKey(
        "accounts.Customer",
        null=True,
        on_delete=models.PROTECT,
        related_name="+",
        verbose_name="auditoriya mijozi",
    )
    audience_phone = models.CharField(
        max_length=13, blank=True, verbose_name="auditoriya telefoni"
    )  # snapshot
    # pushable devices at creation; recomputed at send
    recipient_count = models.PositiveIntegerField(default=0, verbose_name="qabul qiluvchilar soni")
    state = models.CharField(max_length=16, choices=State.choices, default=State.DRAFT, verbose_name="holat")
    # the author's requested send time (optional); the beat's only clock is ``dispatch_after``
    scheduled_at = models.DateTimeField(null=True, verbose_name="so‘ralgan vaqt")
    # Approval workflow: bumps on any change of kind/title/body/image/link/audience; a test or an approval
    # is valid for one revision only.
    content_revision = models.PositiveIntegerField(default=1, verbose_name="matn versiyasi")
    tested_revision = models.PositiveIntegerField(null=True, verbose_name="test tasdiqlangan versiya")
    test_confirmed_at = models.DateTimeField(null=True, verbose_name="test tasdiqlangan")
    test_confirmed_by = models.ForeignKey(
        "accounts.StaffUser",
        null=True,
        on_delete=models.SET_NULL,
        related_name="+",
        verbose_name="testni tasdiqlagan",
    )
    approved_at = models.DateTimeField(null=True, verbose_name="tasdiqlangan")
    approved_by = models.ForeignKey(
        "accounts.StaffUser",
        null=True,
        on_delete=models.SET_NULL,
        related_name="+",
        verbose_name="tasdiqlagan",
    )
    approved_revision = models.PositiveIntegerField(null=True, verbose_name="tasdiqlangan versiya")
    # max(approved_at + CAMPAIGN_APPROVAL_DELAY_MINUTES, scheduled_at): the beat starts the send after it
    dispatch_after = models.DateTimeField(null=True, verbose_name="yuborish vaqti")
    started_at = models.DateTimeField(null=True, verbose_name="boshlangan")
    sent_at = models.DateTimeField(null=True, verbose_name="yuborilgan")
    # M3: set with state cancelled only
    cancelled_at = models.DateTimeField(null=True, verbose_name="bekor qilingan")
    cancelled_by = models.ForeignKey(
        "accounts.StaffUser",
        null=True,
        on_delete=models.SET_NULL,
        related_name="+",
        verbose_name="bekor qilgan",
    )
    stop_requested_at = models.DateTimeField(null=True, verbose_name="to‘xtatish so‘ralgan")
    stop_requested_by = models.ForeignKey(
        "accounts.StaffUser",
        null=True,
        on_delete=models.SET_NULL,
        related_name="+",
        verbose_name="to‘xtatishni so‘ragan",
    )
    stopped_at = models.DateTimeField(null=True, verbose_name="to‘xtatilgan")
    failed_at = models.DateTimeField(null=True, verbose_name="yuborilmagan")
    # live tallies from the first batch on (``skipped`` counts as failed; ``stopped`` counts in neither)
    sent = models.PositiveIntegerField(null=True, verbose_name="yuborildi")  # FCM accepted
    failed = models.PositiveIntegerField(null=True, verbose_name="xato")  # FCM rejected / no token
    # M2 acknowledgements; null in M1
    delivered = models.PositiveIntegerField(null=True, verbose_name="yetkazildi")
    send_cursor = models.BigIntegerField(
        default=0, verbose_name="yuborish kursori"
    )  # last CampaignRecipient.id handed to the adapter
    batches_dispatched = models.PositiveIntegerField(default=0, verbose_name="yuborilgan to‘plamlar")
    last_error = models.TextField(blank=True, verbose_name="oxirgi xato")
    author = models.ForeignKey(
        "accounts.StaffUser",
        null=True,
        on_delete=models.PROTECT,
        related_name="campaigns",
        verbose_name="muallif",
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
                    Q(state="draft") & _NOT_STARTED & _NOT_APPROVED & _NOT_STOPPED
                    | Q(
                        state="tested", tested_revision=F("content_revision"), test_confirmed_at__isnull=False
                    )
                    & _NOT_STARTED
                    & _NOT_APPROVED
                    & _NOT_STOPPED
                    | Q(
                        state="scheduled",
                        approved_at__isnull=False,
                        approved_revision__isnull=False,
                        dispatch_after__isnull=False,
                    )
                    & _NOT_STARTED
                    & _NOT_STOPPED
                    | Q(
                        state="sending",
                        started_at__isnull=False,
                        sent_at__isnull=True,
                        failed_at__isnull=True,
                    )
                    & _NOT_STOPPED
                    | Q(
                        state="stopping",
                        started_at__isnull=False,
                        stop_requested_at__isnull=False,
                        sent_at__isnull=True,
                        stopped_at__isnull=True,
                        failed_at__isnull=True,
                    )
                    | Q(
                        state="sent",
                        started_at__isnull=False,
                        sent_at__isnull=False,
                        sent__isnull=False,
                        failed__isnull=False,
                        stopped_at__isnull=True,
                        failed_at__isnull=True,
                    )
                    | Q(
                        state="partially_sent",
                        started_at__isnull=False,
                        sent__isnull=False,
                        failed__isnull=False,
                        sent_at__isnull=True,
                    )
                    & (Q(stopped_at__isnull=False) | Q(failed_at__isnull=False))
                    | Q(
                        state="failed",
                        started_at__isnull=False,
                        failed_at__isnull=False,
                        sent__isnull=False,
                        failed__isnull=False,
                        sent_at__isnull=True,
                    )
                    | Q(state="cancelled", sent_at__isnull=True, failed_at__isnull=True)
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
            models.Index(
                fields=["dispatch_after"], condition=Q(state="scheduled"), name="campaign_dispatch_idx"
            ),
            models.Index(fields=["state", "kind"], name="campaign_state_kind_idx"),
            models.Index(
                fields=["audience_segment"], condition=Q(state="scheduled"), name="campaign_sched_segment_idx"
            ),
        ]

    def __str__(self) -> str:
        return f"Kampaniya #{self.pk} [{self.get_kind_display()}] {self.title} ({self.get_state_display()})"


class CampaignRecipient(models.Model):
    """One target device of a campaign with its send outcome; the unit of idempotency for batched sends."""

    class State(models.TextChoices):  # internal
        PENDING = "pending", "Kutilmoqda"
        SENT = "sent", "Yuborildi"
        FAILED = "failed", "Xato"
        SKIPPED = "skipped", "O‘tkazib yuborildi"  # device gone / push disabled between materialise and send
        STOPPED = "stopped", "To‘xtatildi"  # never attempted: the campaign stopped / failed before its batch

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
    provider_message_id = models.CharField(
        max_length=128, blank=True, verbose_name="provayder xabar ID"
    )  # FCM message id
    # FCM error / batch
    provider_state = models.JSONField(
        default=dict, blank=True, encoder=DjangoJSONEncoder, verbose_name="provayder holati"
    )
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
        return f"Kampaniya #{self.campaign_id} → mijoz #{self.customer_id} ({self.get_state_display()})"


class TestDevice(TimeStampedModel):
    """A real customer-app device a superuser registered (by the customer's phone) as a test target for
    campaigns; the registry is global and soft-deleted (``is_active``)."""

    device = models.ForeignKey(
        "accounts.Device", on_delete=models.CASCADE, related_name="+", verbose_name="qurilma"
    )
    phone = models.CharField(max_length=13, verbose_name="telefon")  # snapshot of the customer's phone
    label = models.CharField(max_length=60, verbose_name="nomi")  # "Dilnoza — iPhone"
    registered_by = models.ForeignKey(
        "accounts.StaffUser",
        null=True,
        on_delete=models.SET_NULL,
        related_name="+",
        verbose_name="ro‘yxatga olgan",
    )
    is_active = models.BooleanField(default=True, verbose_name="faol")

    class Meta:
        verbose_name = "test qurilmasi"
        verbose_name_plural = "test qurilmalari"
        ordering = ["id"]
        constraints = [
            models.UniqueConstraint(
                fields=["device"], condition=Q(is_active=True), name="testdevice_active_device_unique"
            ),
            models.CheckConstraint(condition=~Q(label=""), name="testdevice_label_required"),
        ]

    def __str__(self) -> str:
        return f"Test qurilmasi «{self.label}» ({self.phone})"


class CampaignTest(models.Model):
    """One test push of a campaign revision to one test device, with the FCM outcome and the superuser's
    confirmation that it looked right on the device."""

    campaign = models.ForeignKey(
        "notifications.Campaign", on_delete=models.CASCADE, related_name="tests", verbose_name="kampaniya"
    )
    revision = models.PositiveIntegerField(verbose_name="matn versiyasi")
    test_device = models.ForeignKey(
        "notifications.TestDevice",
        null=True,
        on_delete=models.SET_NULL,
        related_name="tests",
        verbose_name="test qurilmasi",
    )
    device = models.ForeignKey(
        "accounts.Device", null=True, on_delete=models.SET_NULL, related_name="+", verbose_name="qurilma"
    )
    device_label = models.CharField(max_length=60, verbose_name="qurilma nomi")  # snapshot
    phone = models.CharField(max_length=13, verbose_name="telefon")  # snapshot
    inbox = models.OneToOneField(
        "notifications.InboxNotification",
        null=True,
        on_delete=models.SET_NULL,
        related_name="campaign_test",
        verbose_name="inbox xabari",
    )
    sent_by = models.ForeignKey(
        "accounts.StaffUser",
        null=True,
        on_delete=models.SET_NULL,
        related_name="+",
        verbose_name="yuborgan",
    )
    sent_at = models.DateTimeField(default=timestamp_default, editable=False, verbose_name="yuborilgan")
    # null while the FCM call is in flight; True = accepted; False = rejected / provider down / no token
    fcm_ok = models.BooleanField(null=True, verbose_name="FCM qabul qildi")
    fcm_error = models.CharField(max_length=254, blank=True, verbose_name="FCM xatosi")
    provider_message_id = models.CharField(max_length=128, blank=True, verbose_name="provayder xabar ID")
    confirmed_by = models.ForeignKey(
        "accounts.StaffUser",
        null=True,
        on_delete=models.SET_NULL,
        related_name="+",
        verbose_name="tasdiqlagan",
    )
    confirmed_at = models.DateTimeField(null=True, verbose_name="tasdiqlangan")

    class Meta:
        verbose_name = "kampaniya testi"
        verbose_name_plural = "kampaniya testlari"
        ordering = ["campaign_id", "id"]
        constraints = [
            models.CheckConstraint(
                condition=Q(confirmed_at__isnull=True) | Q(fcm_ok=True),
                name="campaigntest_confirmed_delivered",
            ),
        ]
        indexes = [models.Index(fields=["campaign", "revision"], name="campaigntest_campaign_rev_idx")]

    def __str__(self) -> str:
        return f"Kampaniya #{self.campaign_id} testi → {self.device_label} (v{self.revision})"


class InboxNotification(TimeStampedModel):
    """The customer's in-app feed: one row per message actually addressed to the customer, de-duplicated by
    ``dedupe_key`` so at-least-once handlers never double-notify."""

    class Kind(models.TextChoices):  # internal
        PROMO = "promo", "Aksiya"
        GENERAL = "general", "Umumiy"
        ORDER_STATUS = "order_status", "Buyurtma holati"
        APPOINTMENT = "appointment", "Qabul"

    class SourceType(models.TextChoices):  # internal
        CAMPAIGN = "campaign", "Kampaniya"
        ORDER = "order", "Buyurtma"
        APPOINTMENT = "appointment", "Qabul"
        # M2 account_security notices (customer contract §2.12): source_id = the customer id.
        ACCOUNT = "account", "Hisob"

    class PushState(models.TextChoices):  # internal
        NO_DEVICE = "no_device", "Qurilma yo‘q"
        QUEUED = "queued", "Navbatda"
        SENDING = "sending", "Yuborilmoqda"  # claimed before the FCM call (crash → never resent)
        SENT = "sent", "Yuborildi"
        FAILED = "failed", "Xato"

    customer = models.ForeignKey(
        "accounts.Customer", on_delete=models.CASCADE, related_name="inbox", verbose_name="mijoz"
    )
    kind = models.CharField(max_length=16, choices=Kind.choices, verbose_name="turi")
    title = models.CharField(max_length=80, verbose_name="sarlavha")
    body = models.CharField(max_length=300, verbose_name="matn")
    # {"kind":"none"} | {"kind":"product","product_id":4} | {"kind":"order","order_id":10248} |
    link = models.JSONField(default=dict, encoder=DjangoJSONEncoder, verbose_name="havola")
    # {"kind":"url","url":"https://…"} | …
    image_url = models.CharField(max_length=500, blank=True, verbose_name="rasm havolasi")
    source_type = models.CharField(max_length=16, choices=SourceType.choices, verbose_name="manba turi")
    source_id = models.BigIntegerField(
        verbose_name="manba ID"
    )  # campaign / order / appointment / customer (account) id
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
        return f"Inbox «{self.get_kind_display()}» → mijoz #{self.customer_id}: {self.title[:40]}"
