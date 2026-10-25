"""SiteSettings singleton (design 01 §3); the pk-1 row is inserted by 0002."""

import django.db.models.deletion
from django.db import migrations, models

import glowderma.core.models


class Migration(migrations.Migration):
    initial = True

    dependencies = [
        ("accounts", "0001_initial"),
    ]

    operations = [
        migrations.CreateModel(
            name="SiteSettings",
            fields=[
                (
                    "id",
                    models.PositiveSmallIntegerField(
                        default=1, editable=False, primary_key=True, serialize=False
                    ),
                ),
                ("reservation_minutes", models.PositiveSmallIntegerField(default=15)),
                ("support_phone", models.CharField(blank=True, max_length=13)),
                ("pickup_address_uz", models.CharField(blank=True, max_length=200)),
                ("pickup_address_ru", models.CharField(blank=True, max_length=200)),
                ("pickup_address_en", models.CharField(blank=True, max_length=200)),
                ("cosmetologist_phone", models.CharField(blank=True, max_length=13)),
                ("store_phone", models.CharField(blank=True, max_length=13)),
                ("telegram_url", models.URLField(blank=True)),
                ("instagram_url", models.URLField(blank=True)),
                ("website_url", models.URLField(blank=True)),
                ("privacy_policy_uz", models.TextField(blank=True)),
                ("privacy_policy_ru", models.TextField(blank=True)),
                ("privacy_policy_en", models.TextField(blank=True)),
                ("terms_uz", models.TextField(blank=True)),
                ("terms_ru", models.TextField(blank=True)),
                ("terms_en", models.TextField(blank=True)),
                ("terms_version", models.CharField(blank=True, max_length=32)),
                ("terms_updated_at", models.DateTimeField(null=True)),
                ("booking_policy_uz", models.TextField(blank=True)),
                ("booking_policy_ru", models.TextField(blank=True)),
                ("booking_policy_en", models.TextField(blank=True)),
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
                "verbose_name": "site settings",
                "verbose_name_plural": "site settings",
                "constraints": [
                    models.CheckConstraint(condition=models.Q(("id", 1)), name="sitesettings_singleton"),
                    models.CheckConstraint(
                        condition=models.Q(
                            ("reservation_minutes__gte", 5),
                            ("reservation_minutes__lte", 60),
                        ),
                        name="sitesettings_reservation_5_60",
                    ),
                    models.CheckConstraint(
                        condition=models.Q(
                            ("support_phone", ""),
                            ("support_phone__regex", "^\\+998\\d{9}$"),
                            _connector="OR",
                        ),
                        name="sitesettings_support_phone_e164",
                    ),
                    models.CheckConstraint(
                        condition=models.Q(
                            ("cosmetologist_phone", ""),
                            ("cosmetologist_phone__regex", "^\\+998\\d{9}$"),
                            _connector="OR",
                        ),
                        name="sitesettings_cosmetologist_phone_e164",
                    ),
                    models.CheckConstraint(
                        condition=models.Q(
                            ("store_phone", ""),
                            ("store_phone__regex", "^\\+998\\d{9}$"),
                            _connector="OR",
                        ),
                        name="sitesettings_store_phone_e164",
                    ),
                ],
            },
        ),
    ]
