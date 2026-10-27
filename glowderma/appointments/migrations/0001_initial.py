"""appointments.0001_initial — Appointment (btree_gist exclusion over the open statuses, raw duration check)
and AppointmentEvent (design 03 §2.1, §2.2, §9).

The duration check ``"end" - start = make_interval(mins => duration_min)`` is the one constraint the ORM
cannot express, so it is a ``RunSQL`` after the ORM constraints (``end`` is a reserved word: raw SQL must
quote it). The append-only trigger of ``AppointmentEvent`` is ``0002_appointmentevent_append_only``.
"""

import django.contrib.postgres.constraints
import django.contrib.postgres.fields.ranges
import django.core.serializers.json
import django.db.models.deletion
from django.db import migrations, models

import glowderma.appointments.models
import glowderma.core.models


class Migration(migrations.Migration):
    initial = True

    # accounts.0004 holds Customer (the booking customer) and StaffUser; professionals.0001 the professional
    # and service; core.0007 created btree_gist, which the exclusion constraint needs (design 03 §2.1, §9).
    dependencies = [
        ("accounts", "0004_customer_device_address_smscode_social"),
        ("professionals", "0001_initial"),
        ("core", "0007_db_extensions"),
    ]

    operations = [
        migrations.CreateModel(
            name="Appointment",
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
                ("start", models.DateTimeField()),
                ("end", models.DateTimeField()),
                ("duration_min", models.PositiveSmallIntegerField()),
                ("price", models.BigIntegerField()),
                (
                    "status",
                    models.CharField(
                        choices=[
                            ("requested", "Requested"),
                            ("confirmed", "Confirmed"),
                            ("rejected", "Rejected"),
                            ("attended", "Attended"),
                            ("cancelled", "Cancelled"),
                        ],
                        default="requested",
                        max_length=16,
                    ),
                ),
                (
                    "requested_via",
                    models.CharField(choices=[("app", "App"), ("admin", "Admin")], max_length=8),
                ),
                (
                    "requested_at",
                    models.DateTimeField(default=glowderma.core.models.timestamp_default),
                ),
                ("customer_note", models.CharField(blank=True, max_length=300)),
                ("staff_note", models.CharField(blank=True, max_length=300)),
                ("confirmed_at", models.DateTimeField(null=True)),
                ("rejected_at", models.DateTimeField(null=True)),
                ("attended_at", models.DateTimeField(null=True)),
                ("cancelled_at", models.DateTimeField(null=True)),
                (
                    "cancelled_by",
                    models.CharField(
                        blank=True,
                        choices=[("customer", "Customer"), ("admin", "Admin")],
                        max_length=8,
                    ),
                ),
                ("reschedule_count", models.PositiveSmallIntegerField(default=0)),
                (
                    "created_by",
                    models.ForeignKey(
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="created_appointments",
                        to="accounts.staffuser",
                    ),
                ),
                (
                    "customer",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="appointments",
                        to="accounts.customer",
                    ),
                ),
                (
                    "professional",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="appointments",
                        to="professionals.professional",
                    ),
                ),
                (
                    "service",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="appointments",
                        to="professionals.service",
                    ),
                ),
            ],
            options={
                "ordering": ["start", "id"],
            },
        ),
        migrations.CreateModel(
            name="AppointmentEvent",
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
                    "type",
                    models.CharField(
                        choices=[
                            ("requested", "Requested"),
                            ("confirmed", "Confirmed"),
                            ("rejected", "Rejected"),
                            ("attended", "Attended"),
                            ("cancelled", "Cancelled"),
                            ("rescheduled", "Rescheduled"),
                        ],
                        max_length=16,
                    ),
                ),
                (
                    "source",
                    models.CharField(
                        choices=[
                            ("system", "System"),
                            ("admin", "Admin"),
                            ("customer", "Customer"),
                        ],
                        max_length=8,
                    ),
                ),
                (
                    "data",
                    models.JSONField(
                        default=dict,
                        encoder=django.core.serializers.json.DjangoJSONEncoder,
                    ),
                ),
                (
                    "at",
                    models.DateTimeField(default=glowderma.core.models.timestamp_default, editable=False),
                ),
                (
                    "actor",
                    models.ForeignKey(
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="appointment_events",
                        to="accounts.staffuser",
                    ),
                ),
                (
                    "appointment",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="events",
                        to="appointments.appointment",
                    ),
                ),
            ],
            options={
                "ordering": ["-at", "-id"],
            },
        ),
        migrations.AddIndex(
            model_name="appointment",
            index=models.Index(fields=["professional", "start"], name="appointment_prof_start_idx"),
        ),
        migrations.AddIndex(
            model_name="appointment",
            index=models.Index(fields=["start"], name="appointment_start_idx"),
        ),
        migrations.AddIndex(
            model_name="appointment",
            index=models.Index(
                condition=models.Q(("status", "requested")),
                fields=["start"],
                name="appointment_req_start_idx",
            ),
        ),
        migrations.AddIndex(
            model_name="appointment",
            index=models.Index(
                condition=models.Q(("status__in", ["requested", "confirmed"])),
                fields=["customer", "start"],
                name="appointment_customer_open_idx",
            ),
        ),
        migrations.AddConstraint(
            model_name="appointment",
            constraint=models.CheckConstraint(
                condition=models.Q(("end__gt", models.F("start"))),
                name="appointment_end_after_start",
            ),
        ),
        migrations.AddConstraint(
            model_name="appointment",
            constraint=models.CheckConstraint(
                condition=models.Q(("duration_min__gte", 5), ("duration_min__lte", 480)),
                name="appointment_duration_5_480",
            ),
        ),
        migrations.AddConstraint(
            model_name="appointment",
            constraint=models.CheckConstraint(
                condition=models.Q(("price__gte", 0)),
                name="appointment_price_non_negative",
            ),
        ),
        migrations.AddConstraint(
            model_name="appointment",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    models.Q(("created_by__isnull", False), ("requested_via", "admin")),
                    models.Q(("created_by__isnull", True), ("requested_via", "app")),
                    _connector="OR",
                ),
                name="appointment_created_by_via",
            ),
        ),
        migrations.AddConstraint(
            model_name="appointment",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    models.Q(
                        ("attended_at__isnull", True),
                        ("cancelled_at__isnull", True),
                        ("cancelled_by", ""),
                        ("confirmed_at__isnull", True),
                        ("rejected_at__isnull", True),
                        ("status", "requested"),
                    ),
                    models.Q(
                        ("attended_at__isnull", True),
                        ("cancelled_at__isnull", True),
                        ("cancelled_by", ""),
                        ("confirmed_at__isnull", False),
                        ("rejected_at__isnull", True),
                        ("status", "confirmed"),
                    ),
                    models.Q(
                        ("attended_at__isnull", True),
                        ("cancelled_at__isnull", True),
                        ("cancelled_by", ""),
                        ("confirmed_at__isnull", True),
                        ("rejected_at__isnull", False),
                        ("status", "rejected"),
                    ),
                    models.Q(
                        ("attended_at__isnull", False),
                        ("cancelled_at__isnull", True),
                        ("cancelled_by", ""),
                        ("confirmed_at__isnull", False),
                        ("rejected_at__isnull", True),
                        ("status", "attended"),
                    ),
                    models.Q(
                        ("attended_at__isnull", True),
                        ("cancelled_at__isnull", False),
                        ("rejected_at__isnull", True),
                        ("status", "cancelled"),
                        models.Q(("cancelled_by", ""), _negated=True),
                    ),
                    _connector="OR",
                ),
                name="appointment_status_fields",
            ),
        ),
        migrations.AddConstraint(
            model_name="appointment",
            constraint=django.contrib.postgres.constraints.ExclusionConstraint(
                condition=models.Q(("status__in", ["requested", "confirmed"])),
                expressions=[
                    (
                        glowderma.appointments.models.TsTzRange(
                            "start",
                            "end",
                            django.contrib.postgres.fields.ranges.RangeBoundary(),
                        ),
                        "&&",
                    ),
                    ("professional", "="),
                ],
                name="appointment_no_overlap",
            ),
        ),
        migrations.AddIndex(
            model_name="appointmentevent",
            index=models.Index(fields=["appointment", "at", "id"], name="apptevent_appointment_at_idx"),
        ),
        migrations.AddConstraint(
            model_name="appointmentevent",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    models.Q(("actor__isnull", False), ("source", "admin")),
                    models.Q(
                        models.Q(("source", "admin"), _negated=True),
                        ("actor__isnull", True),
                    ),
                    _connector="OR",
                ),
                name="apptevent_actor_by_source",
            ),
        ),
        migrations.AddConstraint(
            model_name="appointmentevent",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    models.Q(
                        ("source", "admin"),
                        (
                            "type__in",
                            ["confirmed", "rejected", "attended", "rescheduled"],
                        ),
                    ),
                    ("type__in", ["requested", "cancelled"]),
                    _connector="OR",
                ),
                name="apptevent_source_by_type",
            ),
        ),
        migrations.RunSQL(
            sql=(
                "ALTER TABLE appointments_appointment ADD CONSTRAINT appointment_duration_matches "
                'CHECK ("end" - start = make_interval(mins => duration_min));'
            ),
            reverse_sql=(
                "ALTER TABLE appointments_appointment DROP CONSTRAINT IF EXISTS appointment_duration_matches;"
            ),
        ),
    ]
