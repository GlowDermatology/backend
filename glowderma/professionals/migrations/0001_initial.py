"""professionals.0001_initial — Location, Professional, Service, ProfessionalService, WorkingHours,
WorkingHoursOverride, BlockedPeriod (design 01 §5, §7)."""

import django.db.models.deletion
from django.db import migrations, models

import glowderma.core.models
import glowderma.professionals.media


class Migration(migrations.Migration):
    initial = True

    # accounts.0001 holds StaffUser (created_by on overrides and blocks); never a later accounts migration —
    # those are created by a parallel package (design 01 §7 names accounts.0004, see the B2 report).
    dependencies = [("accounts", "0001_initial")]

    operations = [
        migrations.CreateModel(
            name="Location",
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
                ("name_uz", models.CharField(max_length=120)),
                ("name_ru", models.CharField(blank=True, max_length=120)),
                ("name_en", models.CharField(blank=True, max_length=120)),
                ("address_uz", models.CharField(blank=True, max_length=300)),
                ("address_ru", models.CharField(blank=True, max_length=300)),
                ("address_en", models.CharField(blank=True, max_length=300)),
                ("phone", models.CharField(blank=True, max_length=13)),
                (
                    "latitude",
                    models.DecimalField(decimal_places=6, max_digits=9, null=True),
                ),
                (
                    "longitude",
                    models.DecimalField(decimal_places=6, max_digits=9, null=True),
                ),
                ("is_active", models.BooleanField(default=True)),
                ("position", models.PositiveIntegerField(default=0)),
            ],
            options={
                "ordering": ["position", "id"],
                "constraints": [
                    models.CheckConstraint(
                        condition=models.Q(("name_uz", ""), _negated=True),
                        name="location_name_uz_required",
                    ),
                    models.CheckConstraint(
                        condition=models.Q(
                            ("phone", ""),
                            ("phone__regex", "^\\+998\\d{9}$"),
                            _connector="OR",
                        ),
                        name="location_phone_e164",
                    ),
                    models.CheckConstraint(
                        condition=models.Q(
                            models.Q(("latitude__isnull", True), ("longitude__isnull", True)),
                            models.Q(
                                ("latitude__isnull", False),
                                ("longitude__isnull", False),
                            ),
                            _connector="OR",
                        ),
                        name="location_latlng_pair",
                    ),
                ],
            },
        ),
        migrations.CreateModel(
            name="Professional",
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
                ("name", models.CharField(max_length=150)),
                (
                    "role",
                    models.CharField(
                        choices=[
                            ("dermatologist", "Dermatologist"),
                            ("cosmetologist", "Cosmetologist"),
                        ],
                        max_length=16,
                    ),
                ),
                (
                    "photo",
                    models.ImageField(
                        blank=True,
                        max_length=255,
                        upload_to=glowderma.professionals.media.professional_photo_path,
                    ),
                ),
                ("bio_uz", models.TextField(blank=True)),
                ("bio_ru", models.TextField(blank=True)),
                ("bio_en", models.TextField(blank=True)),
                ("experience_years", models.PositiveSmallIntegerField(null=True)),
                ("slot_step_min", models.PositiveSmallIntegerField(default=30)),
                ("position", models.PositiveIntegerField(default=0)),
                ("is_active", models.BooleanField(default=True)),
                (
                    "location",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="professionals",
                        to="professionals.location",
                    ),
                ),
            ],
            options={
                "ordering": ["position", "name", "id"],
            },
        ),
        migrations.CreateModel(
            name="BlockedPeriod",
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
                ("start", models.DateTimeField()),
                ("end", models.DateTimeField()),
                (
                    "kind",
                    models.CharField(choices=[("busy", "Busy"), ("off", "Off")], max_length=8),
                ),
                ("label", models.CharField(blank=True, max_length=80)),
                (
                    "created_at",
                    models.DateTimeField(default=glowderma.core.models.timestamp_default, editable=False),
                ),
                (
                    "created_by",
                    models.ForeignKey(
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="+",
                        to="accounts.staffuser",
                    ),
                ),
                (
                    "professional",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="blocked_periods",
                        to="professionals.professional",
                    ),
                ),
            ],
            options={
                "ordering": ["start", "id"],
            },
        ),
        migrations.CreateModel(
            name="Service",
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
                ("name_uz", models.CharField(max_length=120)),
                ("name_ru", models.CharField(blank=True, max_length=120)),
                ("name_en", models.CharField(blank=True, max_length=120)),
                ("short_name_uz", models.CharField(max_length=40)),
                ("short_name_ru", models.CharField(blank=True, max_length=40)),
                ("short_name_en", models.CharField(blank=True, max_length=40)),
                ("description_uz", models.TextField(blank=True)),
                ("description_ru", models.TextField(blank=True)),
                ("description_en", models.TextField(blank=True)),
                ("default_duration_min", models.PositiveSmallIntegerField()),
                ("default_price", models.BigIntegerField(null=True)),
                ("position", models.PositiveIntegerField(default=0)),
                ("is_active", models.BooleanField(default=True)),
            ],
            options={
                "ordering": ["position", "id"],
                "constraints": [
                    models.CheckConstraint(
                        condition=models.Q(
                            models.Q(("name_uz", ""), _negated=True),
                            models.Q(("short_name_uz", ""), _negated=True),
                        ),
                        name="service_names_uz_required",
                    ),
                    models.CheckConstraint(
                        condition=models.Q(
                            ("default_duration_min__gte", 5),
                            ("default_duration_min__lte", 480),
                        ),
                        name="service_duration_5_480",
                    ),
                    models.CheckConstraint(
                        condition=models.Q(
                            ("default_price__isnull", True),
                            ("default_price__gte", 0),
                            _connector="OR",
                        ),
                        name="service_default_price_non_negative",
                    ),
                ],
            },
        ),
        migrations.CreateModel(
            name="ProfessionalService",
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
                ("price", models.BigIntegerField()),
                ("duration_min", models.PositiveSmallIntegerField()),
                ("is_active", models.BooleanField(default=True)),
                (
                    "created_at",
                    models.DateTimeField(default=glowderma.core.models.timestamp_default, editable=False),
                ),
                (
                    "updated_at",
                    models.DateTimeField(default=glowderma.core.models.timestamp_default),
                ),
                (
                    "professional",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="services",
                        to="professionals.professional",
                    ),
                ),
                (
                    "service",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="professional_links",
                        to="professionals.service",
                    ),
                ),
            ],
            options={
                "ordering": ["professional_id", "service_id"],
            },
        ),
        migrations.CreateModel(
            name="WorkingHours",
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
                ("weekday", models.PositiveSmallIntegerField()),
                ("start", models.TimeField()),
                ("end", models.TimeField()),
                ("break_start", models.TimeField(null=True)),
                ("break_end", models.TimeField(null=True)),
                (
                    "professional",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="working_hours",
                        to="professionals.professional",
                    ),
                ),
            ],
            options={
                "verbose_name_plural": "working hours",
                "ordering": ["professional_id", "weekday"],
            },
        ),
        migrations.CreateModel(
            name="WorkingHoursOverride",
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
                ("date", models.DateField()),
                ("start", models.TimeField(null=True)),
                ("end", models.TimeField(null=True)),
                ("note", models.CharField(blank=True, max_length=120)),
                (
                    "created_at",
                    models.DateTimeField(default=glowderma.core.models.timestamp_default, editable=False),
                ),
                (
                    "created_by",
                    models.ForeignKey(
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="+",
                        to="accounts.staffuser",
                    ),
                ),
                (
                    "professional",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="hours_overrides",
                        to="professionals.professional",
                    ),
                ),
            ],
            options={
                "ordering": ["date"],
            },
        ),
        migrations.AddIndex(
            model_name="professional",
            index=models.Index(
                fields=["is_active", "position", "name"],
                name="prof_professional_active_pos",
            ),
        ),
        migrations.AddConstraint(
            model_name="professional",
            constraint=models.CheckConstraint(
                condition=models.Q(("name", ""), _negated=True),
                name="professional_name_required",
            ),
        ),
        migrations.AddConstraint(
            model_name="professional",
            constraint=models.CheckConstraint(
                condition=models.Q(("slot_step_min__in", [10, 15, 20, 30, 60])),
                name="professional_slot_step_allowed",
            ),
        ),
        migrations.AddIndex(
            model_name="blockedperiod",
            index=models.Index(fields=["professional", "start", "end"], name="prof_blocked_prof_range"),
        ),
        migrations.AddConstraint(
            model_name="blockedperiod",
            constraint=models.CheckConstraint(
                condition=models.Q(("end__gt", models.F("start"))),
                name="blockedperiod_end_after_start",
            ),
        ),
        migrations.AddConstraint(
            model_name="professionalservice",
            constraint=models.UniqueConstraint(
                fields=("professional", "service"), name="professionalservice_unique"
            ),
        ),
        migrations.AddConstraint(
            model_name="professionalservice",
            constraint=models.CheckConstraint(
                condition=models.Q(("price__gte", 0)),
                name="professionalservice_price_non_negative",
            ),
        ),
        migrations.AddConstraint(
            model_name="professionalservice",
            constraint=models.CheckConstraint(
                condition=models.Q(("duration_min__gte", 5), ("duration_min__lte", 480)),
                name="professionalservice_duration_5_480",
            ),
        ),
        migrations.AddConstraint(
            model_name="workinghours",
            constraint=models.UniqueConstraint(
                fields=("professional", "weekday"),
                name="workinghours_professional_weekday_unique",
            ),
        ),
        migrations.AddConstraint(
            model_name="workinghours",
            constraint=models.CheckConstraint(
                condition=models.Q(("weekday__gte", 0), ("weekday__lte", 6)),
                name="workinghours_weekday_0_6",
            ),
        ),
        migrations.AddConstraint(
            model_name="workinghours",
            constraint=models.CheckConstraint(
                condition=models.Q(("start__lt", models.F("end"))),
                name="workinghours_start_before_end",
            ),
        ),
        migrations.AddConstraint(
            model_name="workinghours",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    models.Q(("break_end__isnull", True), ("break_start__isnull", True)),
                    models.Q(
                        ("break_end__isnull", False),
                        ("break_start__isnull", False),
                        ("break_end__lte", models.F("end")),
                        ("break_start__gte", models.F("start")),
                        ("break_start__lt", models.F("break_end")),
                    ),
                    _connector="OR",
                ),
                name="workinghours_break_inside_day",
            ),
        ),
        migrations.AddConstraint(
            model_name="workinghoursoverride",
            constraint=models.UniqueConstraint(
                fields=("professional", "date"),
                name="hoursoverride_professional_date_unique",
            ),
        ),
        migrations.AddConstraint(
            model_name="workinghoursoverride",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    models.Q(("end__isnull", True), ("start__isnull", True)),
                    models.Q(
                        ("end__isnull", False),
                        ("start__isnull", False),
                        ("start__lt", models.F("end")),
                    ),
                    _connector="OR",
                ),
                name="hoursoverride_both_or_none",
            ),
        ),
    ]
