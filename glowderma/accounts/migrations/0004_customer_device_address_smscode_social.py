"""Customer, Device, Address, SmsCode and CustomerSocialIdentity (design 01 §1.1–§1.5).

The trigram GIN index on LOWER(name) needs ``pg_trgm`` (core.0007); the dependency is on core.0008 so every
migration of this slice sits above the GUC-aware append-only trigger function.
"""

import django.contrib.postgres.indexes
import django.db.models.deletion
import django.db.models.functions.text
from django.db import migrations, models

import glowderma.accounts.media
import glowderma.core.models


class Migration(migrations.Migration):
    dependencies = [
        ("accounts", "0003_sessions_socialidentity"),
        ("core", "0008_forbid_mutation_purge_guc"),
    ]

    operations = [
        migrations.CreateModel(
            name="SmsCode",
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
                ("phone", models.CharField(max_length=13)),
                (
                    "purpose",
                    models.CharField(
                        choices=[
                            ("signup", "Signup"),
                            ("reset", "Reset"),
                            ("new_device", "New Device"),
                            ("social_link", "Social Link"),
                        ],
                        max_length=20,
                    ),
                ),
                ("code_hmac", models.CharField(max_length=64)),
                ("attempts", models.PositiveSmallIntegerField(default=0)),
                ("max_attempts", models.PositiveSmallIntegerField(default=5)),
                (
                    "created_at",
                    models.DateTimeField(default=glowderma.core.models.timestamp_default),
                ),
                ("expires_at", models.DateTimeField()),
                ("sent_at", models.DateTimeField(null=True)),
                ("consumed_at", models.DateTimeField(null=True)),
                ("exhausted_at", models.DateTimeField(null=True)),
                (
                    "verification_token_hash",
                    models.CharField(blank=True, max_length=64),
                ),
                ("verification_expires_at", models.DateTimeField(null=True)),
                ("verification_used_at", models.DateTimeField(null=True)),
                ("request_ip", models.GenericIPAddressField(null=True)),
                ("provider_message_id", models.CharField(blank=True, max_length=64)),
            ],
        ),
        migrations.CreateModel(
            name="Customer",
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
                ("phone", models.CharField(max_length=13, unique=True)),
                ("name", models.CharField(blank=True, max_length=150)),
                (
                    "language",
                    models.CharField(
                        choices=[("uz", "Uz"), ("ru", "Ru"), ("en", "En")],
                        default="uz",
                        max_length=2,
                    ),
                ),
                (
                    "status",
                    models.CharField(
                        choices=[("active", "Active"), ("deleted", "Deleted")],
                        db_index=True,
                        default="active",
                        max_length=8,
                    ),
                ),
                (
                    "image",
                    models.ImageField(
                        blank=True,
                        max_length=255,
                        upload_to=glowderma.accounts.media.customer_image_path,
                    ),
                ),
                ("address_text", models.CharField(blank=True, max_length=300)),
                (
                    "registered_at",
                    models.DateTimeField(db_index=True, default=glowderma.core.models.timestamp_default),
                ),
                ("deleted_at", models.DateTimeField(null=True)),
                ("reactivated_at", models.DateTimeField(null=True)),
                ("last_login_at", models.DateTimeField(null=True)),
                ("password", models.CharField(blank=True, max_length=128)),
                ("password_changed_at", models.DateTimeField(null=True)),
                ("terms_version", models.CharField(blank=True, max_length=32)),
                ("consent_at", models.DateTimeField(null=True)),
                ("consent_ip", models.GenericIPAddressField(null=True)),
            ],
            options={
                "ordering": ["-registered_at", "-id"],
                "indexes": [
                    django.contrib.postgres.indexes.GinIndex(
                        django.contrib.postgres.indexes.OpClass(
                            django.db.models.functions.text.Lower("name"),
                            name="gin_trgm_ops",
                        ),
                        name="accounts_customer_name_trgm",
                    ),
                    models.Index(
                        fields=["status", "registered_at"],
                        name="accounts_customer_status_reg",
                    ),
                ],
                "constraints": [
                    models.CheckConstraint(
                        condition=models.Q(("phone__regex", "^\\+998\\d{9}$")),
                        name="customer_phone_e164",
                    ),
                    models.CheckConstraint(
                        condition=models.Q(
                            models.Q(("deleted_at__isnull", True), ("status", "active")),
                            models.Q(("deleted_at__isnull", False), ("status", "deleted")),
                            _connector="OR",
                        ),
                        name="customer_status_deleted_at_consistent",
                    ),
                    models.CheckConstraint(
                        condition=models.Q(
                            ("consent_at__isnull", True),
                            models.Q(("terms_version", ""), _negated=True),
                            _connector="OR",
                        ),
                        name="customer_consent_has_terms_version",
                    ),
                ],
            },
        ),
        migrations.CreateModel(
            name="Address",
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
                ("label", models.CharField(blank=True, max_length=60)),
                ("text", models.CharField(max_length=300)),
                ("region_code", models.CharField(blank=True, max_length=16)),
                ("city_code", models.CharField(blank=True, max_length=16)),
                ("comment", models.CharField(blank=True, max_length=200)),
                (
                    "latitude",
                    models.DecimalField(decimal_places=6, max_digits=9, null=True),
                ),
                (
                    "longitude",
                    models.DecimalField(decimal_places=6, max_digits=9, null=True),
                ),
                ("is_default", models.BooleanField(default=False)),
                (
                    "customer",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="addresses",
                        to="accounts.customer",
                    ),
                ),
            ],
            options={
                "ordering": ["-is_default", "-updated_at"],
            },
        ),
        migrations.CreateModel(
            name="CustomerSocialIdentity",
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
                    "provider",
                    models.CharField(
                        choices=[("google", "google"), ("facebook", "facebook")],
                        max_length=16,
                    ),
                ),
                ("subject", models.CharField(max_length=254)),
                ("email_at_link", models.EmailField(blank=True, max_length=254)),
                (
                    "created_at",
                    models.DateTimeField(default=glowderma.core.models.timestamp_default, editable=False),
                ),
                ("last_login_at", models.DateTimeField(null=True)),
                (
                    "customer",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="social_identities",
                        to="accounts.customer",
                    ),
                ),
            ],
        ),
        migrations.CreateModel(
            name="Device",
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
                ("fid", models.CharField(max_length=128)),
                (
                    "platform",
                    models.CharField(choices=[("ios", "Ios"), ("android", "Android")], max_length=8),
                ),
                ("model", models.CharField(blank=True, max_length=80)),
                ("os", models.CharField(blank=True, max_length=40)),
                ("app_version", models.CharField(blank=True, max_length=20)),
                ("locale", models.CharField(blank=True, max_length=8)),
                ("fcm_token", models.CharField(blank=True, max_length=512)),
                ("push_enabled", models.BooleanField(default=False)),
                ("is_active", models.BooleanField(default=True)),
                (
                    "last_active_at",
                    models.DateTimeField(default=glowderma.core.models.timestamp_default),
                ),
                ("fcm_invalidated_at", models.DateTimeField(null=True)),
                (
                    "customer",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="devices",
                        to="accounts.customer",
                    ),
                ),
            ],
        ),
        migrations.AddIndex(
            model_name="smscode",
            index=models.Index(
                fields=["phone", "purpose", "created_at"],
                name="accounts_smscode_phone_purpose",
            ),
        ),
        migrations.AddIndex(
            model_name="smscode",
            index=models.Index(fields=["expires_at"], name="accounts_smscode_expires"),
        ),
        migrations.AddConstraint(
            model_name="smscode",
            constraint=models.CheckConstraint(
                condition=models.Q(("phone__regex", "^\\+998\\d{9}$")),
                name="smscode_phone_e164",
            ),
        ),
        migrations.AddConstraint(
            model_name="smscode",
            constraint=models.CheckConstraint(
                condition=models.Q(("attempts__lte", models.F("max_attempts"))),
                name="smscode_attempts_bound",
            ),
        ),
        migrations.AddConstraint(
            model_name="smscode",
            constraint=models.UniqueConstraint(
                condition=models.Q(("verification_token_hash", ""), _negated=True),
                fields=("verification_token_hash",),
                name="smscode_verification_token_unique",
            ),
        ),
        migrations.AddConstraint(
            model_name="address",
            constraint=models.UniqueConstraint(
                condition=models.Q(("is_default", True)),
                fields=("customer",),
                name="address_one_default_per_customer",
            ),
        ),
        migrations.AddConstraint(
            model_name="address",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    models.Q(("latitude__isnull", True), ("longitude__isnull", True)),
                    models.Q(("latitude__isnull", False), ("longitude__isnull", False)),
                    _connector="OR",
                ),
                name="address_latlng_pair",
            ),
        ),
        migrations.AddConstraint(
            model_name="customersocialidentity",
            constraint=models.UniqueConstraint(
                fields=("provider", "subject"),
                name="customer_social_provider_subject_unique",
            ),
        ),
        migrations.AddConstraint(
            model_name="customersocialidentity",
            constraint=models.UniqueConstraint(
                fields=("customer", "provider"),
                name="customer_social_customer_provider_unique",
            ),
        ),
        migrations.AddIndex(
            model_name="device",
            index=models.Index(
                fields=["customer", "is_active", "last_active_at"],
                name="accounts_device_cust_active",
            ),
        ),
        migrations.AddIndex(
            model_name="device",
            index=models.Index(
                condition=models.Q(("is_active", True), ("push_enabled", True)),
                fields=["push_enabled"],
                name="accounts_device_pushable",
            ),
        ),
        migrations.AddConstraint(
            model_name="device",
            constraint=models.UniqueConstraint(fields=("customer", "fid"), name="device_customer_fid_unique"),
        ),
        migrations.AddConstraint(
            model_name="device",
            constraint=models.UniqueConstraint(
                condition=models.Q(("is_active", True), models.Q(("fcm_token", ""), _negated=True)),
                fields=("fcm_token",),
                name="device_active_fcm_token_unique",
            ),
        ),
        migrations.AddConstraint(
            model_name="device",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    ("push_enabled", False),
                    models.Q(("fcm_token", ""), _negated=True),
                    _connector="OR",
                ),
                name="device_push_requires_token",
            ),
        ),
    ]
