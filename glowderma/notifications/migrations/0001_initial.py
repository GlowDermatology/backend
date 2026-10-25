"""notifications.0001_initial — StatusTemplate, CustomerSegment, SegmentMember, Campaign, CampaignRecipient,
InboxNotification (design 03 §3, §9). The four template rows come with ``0002_seed_status_templates``."""

import django.core.serializers.json
import django.db.models.deletion
import django.db.models.functions.comparison
import django.db.models.functions.text
from django.db import migrations, models

import glowderma.core.models
import glowderma.notifications.media


class Migration(migrations.Migration):
    initial = True

    # accounts.0004 holds Customer and Device (recipients, inbox, audiences) and StaffUser (actors);
    # catalog.0001 holds Product (Campaign.link_product). Design 03 §9 row 18.
    dependencies = [
        ("accounts", "0004_customer_device_address_smscode_social"),
        ("catalog", "0001_initial"),
    ]

    operations = [
        migrations.CreateModel(
            name="CustomerSegment",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                (
                    "created_at",
                    models.DateTimeField(default=glowderma.core.models.timestamp_default, editable=False),
                ),
                (
                    "updated_at",
                    models.DateTimeField(default=glowderma.core.models.timestamp_default),
                ),
                ("name", models.CharField(max_length=60)),
                ("min_online_orders", models.PositiveIntegerField(null=True)),
                ("min_lifetime_amount", models.BigIntegerField(null=True)),
                (
                    "created_by",
                    models.ForeignKey(
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="+",
                        to="accounts.staffuser",
                    ),
                ),
            ],
            options={
                "ordering": ["id"],
            },
        ),
        migrations.CreateModel(
            name="Campaign",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                (
                    "created_at",
                    models.DateTimeField(default=glowderma.core.models.timestamp_default, editable=False),
                ),
                (
                    "updated_at",
                    models.DateTimeField(default=glowderma.core.models.timestamp_default),
                ),
                (
                    "kind",
                    models.CharField(
                        choices=[("promo", "Promo"), ("general", "General")],
                        max_length=8,
                    ),
                ),
                ("title", models.CharField(max_length=60)),
                ("body", models.CharField(max_length=180)),
                (
                    "image",
                    models.ImageField(
                        blank=True,
                        max_length=255,
                        upload_to=glowderma.notifications.media.campaign_image_path,
                    ),
                ),
                (
                    "link_kind",
                    models.CharField(
                        choices=[("none", "None"), ("product", "Product")],
                        default="none",
                        max_length=8,
                    ),
                ),
                ("link_product_label", models.CharField(blank=True, max_length=120)),
                (
                    "audience_type",
                    models.CharField(
                        choices=[
                            ("all", "All"),
                            ("segment", "Segment"),
                            ("customer", "Customer"),
                        ],
                        max_length=8,
                    ),
                ),
                ("audience_segment_name", models.CharField(blank=True, max_length=60)),
                ("audience_phone", models.CharField(blank=True, max_length=13)),
                ("recipient_count", models.PositiveIntegerField(default=0)),
                (
                    "state",
                    models.CharField(
                        choices=[
                            ("scheduled", "Scheduled"),
                            ("queued", "Queued"),
                            ("sending", "Sending"),
                            ("sent", "Sent"),
                        ],
                        default="queued",
                        max_length=16,
                    ),
                ),
                ("scheduled_at", models.DateTimeField(null=True)),
                ("started_at", models.DateTimeField(null=True)),
                ("sent_at", models.DateTimeField(null=True)),
                ("sent", models.PositiveIntegerField(null=True)),
                ("failed", models.PositiveIntegerField(null=True)),
                ("delivered", models.PositiveIntegerField(null=True)),
                ("send_cursor", models.BigIntegerField(default=0)),
                ("batches_dispatched", models.PositiveIntegerField(default=0)),
                ("last_error", models.TextField(blank=True)),
                (
                    "audience_customer",
                    models.ForeignKey(
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="+",
                        to="accounts.customer",
                    ),
                ),
                (
                    "author",
                    models.ForeignKey(
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="campaigns",
                        to="accounts.staffuser",
                    ),
                ),
                (
                    "link_product",
                    models.ForeignKey(
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="+",
                        to="catalog.product",
                    ),
                ),
                (
                    "audience_segment",
                    models.ForeignKey(
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="campaigns",
                        to="notifications.customersegment",
                    ),
                ),
            ],
            options={
                "ordering": [
                    models.OrderBy(
                        django.db.models.functions.comparison.Coalesce("scheduled_at", "created_at"),
                        descending=True,
                    ),
                    "-id",
                ],
            },
        ),
        migrations.CreateModel(
            name="InboxNotification",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                (
                    "created_at",
                    models.DateTimeField(default=glowderma.core.models.timestamp_default, editable=False),
                ),
                (
                    "updated_at",
                    models.DateTimeField(default=glowderma.core.models.timestamp_default),
                ),
                (
                    "kind",
                    models.CharField(
                        choices=[
                            ("promo", "Promo"),
                            ("general", "General"),
                            ("order_status", "Order Status"),
                            ("appointment", "Appointment"),
                        ],
                        max_length=16,
                    ),
                ),
                ("title", models.CharField(max_length=80)),
                ("body", models.CharField(max_length=300)),
                (
                    "link",
                    models.JSONField(
                        default=dict,
                        encoder=django.core.serializers.json.DjangoJSONEncoder,
                    ),
                ),
                ("image_url", models.CharField(blank=True, max_length=500)),
                (
                    "source_type",
                    models.CharField(
                        choices=[
                            ("campaign", "Campaign"),
                            ("order", "Order"),
                            ("appointment", "Appointment"),
                        ],
                        max_length=16,
                    ),
                ),
                ("source_id", models.BigIntegerField()),
                (
                    "template_status",
                    models.CharField(
                        blank=True,
                        choices=[
                            ("processing", "Processing"),
                            ("handed_to_courier", "Handed To Courier"),
                            ("delivered", "Delivered"),
                            ("cancelled", "Cancelled"),
                        ],
                        max_length=24,
                    ),
                ),
                (
                    "language",
                    models.CharField(choices=[("uz", "Uz"), ("ru", "Ru"), ("en", "En")], max_length=2),
                ),
                ("dedupe_key", models.CharField(max_length=120, unique=True)),
                (
                    "push_state",
                    models.CharField(
                        choices=[
                            ("no_device", "No Device"),
                            ("queued", "Queued"),
                            ("sending", "Sending"),
                            ("sent", "Sent"),
                            ("failed", "Failed"),
                        ],
                        default="no_device",
                        max_length=16,
                    ),
                ),
                ("pushed_at", models.DateTimeField(null=True)),
                ("push_error", models.CharField(blank=True, max_length=254)),
                ("read_at", models.DateTimeField(null=True)),
                ("delivered_at", models.DateTimeField(null=True)),
                (
                    "customer",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="inbox",
                        to="accounts.customer",
                    ),
                ),
            ],
            options={
                "ordering": ["-created_at", "-id"],
            },
        ),
        migrations.CreateModel(
            name="CampaignRecipient",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                (
                    "state",
                    models.CharField(
                        choices=[
                            ("pending", "Pending"),
                            ("sent", "Sent"),
                            ("failed", "Failed"),
                            ("skipped", "Skipped"),
                        ],
                        default="pending",
                        max_length=8,
                    ),
                ),
                ("error", models.CharField(blank=True, max_length=254)),
                ("provider_message_id", models.CharField(blank=True, max_length=128)),
                (
                    "provider_state",
                    models.JSONField(
                        blank=True,
                        default=dict,
                        encoder=django.core.serializers.json.DjangoJSONEncoder,
                    ),
                ),
                ("sent_at", models.DateTimeField(null=True)),
                ("delivered_at", models.DateTimeField(null=True)),
                (
                    "created_at",
                    models.DateTimeField(default=glowderma.core.models.timestamp_default, editable=False),
                ),
                (
                    "campaign",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="recipients",
                        to="notifications.campaign",
                    ),
                ),
                (
                    "customer",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="+",
                        to="accounts.customer",
                    ),
                ),
                (
                    "device",
                    models.ForeignKey(
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="+",
                        to="accounts.device",
                    ),
                ),
                (
                    "inbox",
                    models.OneToOneField(
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="campaign_recipient",
                        to="notifications.inboxnotification",
                    ),
                ),
            ],
            options={
                "ordering": ["campaign_id", "id"],
            },
        ),
        migrations.CreateModel(
            name="SegmentMember",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                (
                    "added_at",
                    models.DateTimeField(default=glowderma.core.models.timestamp_default, editable=False),
                ),
                (
                    "added_by",
                    models.ForeignKey(
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="+",
                        to="accounts.staffuser",
                    ),
                ),
                (
                    "customer",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="segment_memberships",
                        to="accounts.customer",
                    ),
                ),
                (
                    "segment",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="manual_members",
                        to="notifications.customersegment",
                    ),
                ),
            ],
            options={
                "ordering": ["segment_id", "added_at", "id"],
            },
        ),
        migrations.CreateModel(
            name="StatusTemplate",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                (
                    "status",
                    models.CharField(
                        choices=[
                            ("processing", "Processing"),
                            ("handed_to_courier", "Handed To Courier"),
                            ("delivered", "Delivered"),
                            ("cancelled", "Cancelled"),
                        ],
                        max_length=24,
                        unique=True,
                    ),
                ),
                ("text_uz", models.CharField(max_length=180)),
                ("text_ru", models.CharField(blank=True, max_length=180)),
                ("text_en", models.CharField(blank=True, max_length=180)),
                (
                    "created_at",
                    models.DateTimeField(default=glowderma.core.models.timestamp_default, editable=False),
                ),
                (
                    "updated_at",
                    models.DateTimeField(default=glowderma.core.models.timestamp_default),
                ),
                (
                    "updated_by",
                    models.ForeignKey(
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="+",
                        to="accounts.staffuser",
                    ),
                ),
            ],
            options={
                "ordering": ["id"],
            },
        ),
        migrations.AddConstraint(
            model_name="customersegment",
            constraint=models.UniqueConstraint(
                django.db.models.functions.text.Lower("name"),
                name="segment_name_ci_unique",
            ),
        ),
        migrations.AddConstraint(
            model_name="customersegment",
            constraint=models.CheckConstraint(
                condition=models.Q(("name", ""), _negated=True),
                name="segment_name_required",
            ),
        ),
        migrations.AddConstraint(
            model_name="customersegment",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    ("min_online_orders__isnull", True),
                    ("min_online_orders__gte", 1),
                    _connector="OR",
                ),
                name="segment_min_orders_positive",
            ),
        ),
        migrations.AddConstraint(
            model_name="customersegment",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    ("min_lifetime_amount__isnull", True),
                    ("min_lifetime_amount__gte", 1),
                    _connector="OR",
                ),
                name="segment_min_amount_positive",
            ),
        ),
        migrations.AddIndex(
            model_name="campaign",
            index=models.Index(
                models.OrderBy(
                    django.db.models.functions.comparison.Coalesce("scheduled_at", "created_at"),
                    descending=True,
                ),
                models.OrderBy(models.F("id"), descending=True),
                name="campaign_at_idx",
            ),
        ),
        migrations.AddIndex(
            model_name="campaign",
            index=models.Index(
                condition=models.Q(("state", "scheduled")),
                fields=["scheduled_at"],
                name="campaign_due_idx",
            ),
        ),
        migrations.AddIndex(
            model_name="campaign",
            index=models.Index(fields=["state", "kind"], name="campaign_state_kind_idx"),
        ),
        migrations.AddIndex(
            model_name="campaign",
            index=models.Index(
                condition=models.Q(("state", "scheduled")),
                fields=["audience_segment"],
                name="campaign_sched_segment_idx",
            ),
        ),
        migrations.AddConstraint(
            model_name="campaign",
            constraint=models.CheckConstraint(
                condition=models.Q(("title__regex", "^.{3,60}$"), ("body__regex", "^.{5,180}$")),
                name="campaign_text_lengths",
            ),
        ),
        migrations.AddConstraint(
            model_name="campaign",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    models.Q(
                        ("link_kind", "none"),
                        ("link_product__isnull", True),
                        ("link_product_label", ""),
                    ),
                    models.Q(
                        ("link_kind", "product"),
                        models.Q(("link_product_label", ""), _negated=True),
                    ),
                    _connector="OR",
                ),
                name="campaign_link_fields",
            ),
        ),
        migrations.AddConstraint(
            model_name="campaign",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    models.Q(
                        ("audience_customer__isnull", True),
                        ("audience_phone", ""),
                        ("audience_segment__isnull", True),
                        ("audience_segment_name", ""),
                        ("audience_type", "all"),
                    ),
                    models.Q(
                        ("audience_customer__isnull", True),
                        ("audience_phone", ""),
                        ("audience_type", "segment"),
                        models.Q(("audience_segment_name", ""), _negated=True),
                    ),
                    models.Q(
                        ("audience_customer__isnull", False),
                        ("audience_segment__isnull", True),
                        ("audience_segment_name", ""),
                        ("audience_type", "customer"),
                        ("audience_phone__regex", "^\\+998\\d{9}$"),
                    ),
                    _connector="OR",
                ),
                name="campaign_audience_fields",
            ),
        ),
        migrations.AddConstraint(
            model_name="campaign",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    models.Q(
                        ("failed__isnull", True),
                        ("scheduled_at__isnull", False),
                        ("sent__isnull", True),
                        ("sent_at__isnull", True),
                        ("started_at__isnull", True),
                        ("state", "scheduled"),
                    ),
                    models.Q(
                        ("failed__isnull", True),
                        ("sent__isnull", True),
                        ("sent_at__isnull", True),
                        ("started_at__isnull", True),
                        ("state", "queued"),
                    ),
                    models.Q(
                        ("failed__isnull", True),
                        ("sent__isnull", True),
                        ("sent_at__isnull", True),
                        ("started_at__isnull", False),
                        ("state", "sending"),
                    ),
                    models.Q(
                        ("failed__isnull", False),
                        ("sent__isnull", False),
                        ("sent_at__isnull", False),
                        ("started_at__isnull", False),
                        ("state", "sent"),
                    ),
                    _connector="OR",
                ),
                name="campaign_state_fields",
            ),
        ),
        migrations.AddIndex(
            model_name="inboxnotification",
            index=models.Index(
                fields=["customer", "-created_at", "-id"],
                name="inbox_customer_created_idx",
            ),
        ),
        migrations.AddIndex(
            model_name="inboxnotification",
            index=models.Index(
                condition=models.Q(("read_at__isnull", True)),
                fields=["customer"],
                name="inbox_customer_unread_idx",
            ),
        ),
        migrations.AddIndex(
            model_name="inboxnotification",
            index=models.Index(fields=["source_type", "source_id"], name="inbox_source_idx"),
        ),
        migrations.AddConstraint(
            model_name="inboxnotification",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    models.Q(
                        ("kind", "order_status"),
                        ("source_type", "order"),
                        models.Q(("template_status", ""), _negated=True),
                    ),
                    models.Q(
                        ("kind", "appointment"),
                        ("source_type", "appointment"),
                        ("template_status", ""),
                    ),
                    models.Q(
                        ("kind__in", ["promo", "general"]),
                        ("source_type", "campaign"),
                        ("template_status", ""),
                    ),
                    _connector="OR",
                ),
                name="inbox_kind_source_fields",
            ),
        ),
        migrations.AddConstraint(
            model_name="inboxnotification",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    models.Q(("title", ""), _negated=True),
                    models.Q(("body", ""), _negated=True),
                ),
                name="inbox_text_required",
            ),
        ),
        migrations.AddIndex(
            model_name="campaignrecipient",
            index=models.Index(fields=["campaign", "state"], name="recipient_campaign_state_idx"),
        ),
        migrations.AddIndex(
            model_name="campaignrecipient",
            index=models.Index(fields=["customer", "-created_at"], name="recipient_customer_idx"),
        ),
        migrations.AddConstraint(
            model_name="campaignrecipient",
            constraint=models.UniqueConstraint(
                fields=("campaign", "customer"),
                name="recipient_campaign_customer_unique",
            ),
        ),
        migrations.AddConstraint(
            model_name="campaignrecipient",
            constraint=models.UniqueConstraint(
                condition=models.Q(("device__isnull", False)),
                fields=("campaign", "device"),
                name="recipient_campaign_device_unique",
            ),
        ),
        migrations.AddConstraint(
            model_name="campaignrecipient",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    models.Q(("sent_at__isnull", False), ("state", "sent")),
                    models.Q(
                        models.Q(("state", "sent"), _negated=True),
                        ("sent_at__isnull", True),
                    ),
                    _connector="OR",
                ),
                name="recipient_sent_at",
            ),
        ),
        migrations.AddIndex(
            model_name="segmentmember",
            index=models.Index(fields=["customer"], name="segmentmember_customer_idx"),
        ),
        migrations.AddConstraint(
            model_name="segmentmember",
            constraint=models.UniqueConstraint(fields=("segment", "customer"), name="segmentmember_unique"),
        ),
        migrations.AddConstraint(
            model_name="statustemplate",
            constraint=models.CheckConstraint(
                condition=models.Q(("text_uz__regex", "^.{5,180}$")),
                name="template_text_uz_5_180",
            ),
        ),
        migrations.AddConstraint(
            model_name="statustemplate",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    models.Q(("text_uz__regex", "\\{(?!raqam\\})"), _negated=True),
                    models.Q(("text_ru__regex", "\\{(?!raqam\\})"), _negated=True),
                    models.Q(("text_en__regex", "\\{(?!raqam\\})"), _negated=True),
                ),
                name="template_only_raqam_placeholder",
            ),
        ),
    ]
