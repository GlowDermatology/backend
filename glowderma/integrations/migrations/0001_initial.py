"""WebhookInbox, Job and IntegrationState (design 01 §2); the append-only trigger follows in 0002."""

import django.core.serializers.json
import django.db.models.deletion
from django.db import migrations, models

import glowderma.core.models


class Migration(migrations.Migration):
    initial = True

    dependencies = [
        ("accounts", "0004_customer_device_address_smscode_social"),
    ]

    operations = [
        migrations.CreateModel(
            name="IntegrationState",
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
                        choices=[
                            ("billz", "Billz"),
                            ("bts", "Bts"),
                            ("click", "Click"),
                            ("payme", "Payme"),
                            ("eskiz", "Eskiz"),
                            ("fcm", "Fcm"),
                        ],
                        max_length=16,
                        unique=True,
                    ),
                ),
                ("last_sync_at", models.DateTimeField(null=True)),
                ("last_success_at", models.DateTimeField(null=True)),
                ("last_error_at", models.DateTimeField(null=True)),
                ("last_error", models.TextField(blank=True)),
                ("consecutive_failures", models.PositiveIntegerField(default=0)),
                (
                    "data",
                    models.JSONField(
                        default=dict,
                        encoder=django.core.serializers.json.DjangoJSONEncoder,
                    ),
                ),
                (
                    "updated_at",
                    models.DateTimeField(default=glowderma.core.models.timestamp_default),
                ),
            ],
            options={
                "verbose_name": "integration state",
            },
        ),
        migrations.CreateModel(
            name="WebhookInbox",
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
                        choices=[
                            ("click", "Click"),
                            ("payme", "Payme"),
                            ("bts", "Bts"),
                            ("billz", "Billz"),
                        ],
                        max_length=16,
                    ),
                ),
                ("event_key", models.CharField(max_length=160)),
                (
                    "received_at",
                    models.DateTimeField(default=glowderma.core.models.timestamp_default),
                ),
                ("remote_ip", models.GenericIPAddressField(null=True)),
                ("request_id", models.CharField(blank=True, max_length=64)),
                ("method", models.CharField(max_length=8)),
                ("path", models.CharField(max_length=200)),
                (
                    "headers",
                    models.JSONField(
                        default=dict,
                        encoder=django.core.serializers.json.DjangoJSONEncoder,
                    ),
                ),
                (
                    "payload",
                    models.JSONField(
                        encoder=django.core.serializers.json.DjangoJSONEncoder,
                        null=True,
                    ),
                ),
                ("raw_body", models.TextField(blank=True)),
                ("entity_ref", models.CharField(blank=True, max_length=64)),
                (
                    "result",
                    models.JSONField(encoder=django.core.serializers.json.DjangoJSONEncoder),
                ),
                ("processing_ms", models.PositiveIntegerField(default=0)),
            ],
            options={
                "ordering": ["-received_at", "-id"],
                "indexes": [
                    models.Index(
                        fields=["provider", "received_at"],
                        name="integr_webhook_provider_time",
                    ),
                    models.Index(fields=["entity_ref"], name="integr_webhook_entity_ref"),
                ],
                "constraints": [
                    models.UniqueConstraint(
                        fields=("provider", "event_key"),
                        name="webhookinbox_provider_event_unique",
                    )
                ],
            },
        ),
        migrations.CreateModel(
            name="Job",
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
                    models.CharField(choices=[("billz_import", "Billz Import")], max_length=32),
                ),
                (
                    "state",
                    models.CharField(
                        choices=[
                            ("queued", "Queued"),
                            ("running", "Running"),
                            ("succeeded", "Succeeded"),
                            ("failed", "Failed"),
                        ],
                        db_index=True,
                        default="queued",
                        max_length=16,
                    ),
                ),
                (
                    "params",
                    models.JSONField(
                        default=dict,
                        encoder=django.core.serializers.json.DjangoJSONEncoder,
                    ),
                ),
                (
                    "progress",
                    models.JSONField(
                        encoder=django.core.serializers.json.DjangoJSONEncoder,
                        null=True,
                    ),
                ),
                (
                    "result",
                    models.JSONField(
                        encoder=django.core.serializers.json.DjangoJSONEncoder,
                        null=True,
                    ),
                ),
                (
                    "error",
                    models.JSONField(
                        encoder=django.core.serializers.json.DjangoJSONEncoder,
                        null=True,
                    ),
                ),
                ("started_at", models.DateTimeField(null=True)),
                ("finished_at", models.DateTimeField(null=True)),
                ("heartbeat_at", models.DateTimeField(null=True)),
                ("attempts", models.PositiveSmallIntegerField(default=0)),
                ("celery_task_id", models.CharField(blank=True, max_length=36)),
                (
                    "created_by",
                    models.ForeignKey(
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="jobs",
                        to="accounts.staffuser",
                    ),
                ),
            ],
            options={
                "ordering": ["-created_at", "-id"],
                "indexes": [models.Index(fields=["kind", "created_at"], name="integr_job_kind_created")],
                "constraints": [
                    models.UniqueConstraint(
                        condition=models.Q(("state__in", ["queued", "running"])),
                        fields=("kind",),
                        name="job_single_flight_per_kind",
                    ),
                    models.CheckConstraint(
                        condition=models.Q(
                            models.Q(
                                ("finished_at__isnull", True),
                                ("started_at__isnull", True),
                                ("state", "queued"),
                            ),
                            models.Q(
                                ("finished_at__isnull", True),
                                ("started_at__isnull", False),
                                ("state", "running"),
                            ),
                            models.Q(
                                ("finished_at__isnull", False),
                                ("started_at__isnull", False),
                                ("state__in", ["succeeded", "failed"]),
                            ),
                            _connector="OR",
                        ),
                        name="job_state_timestamps_consistent",
                    ),
                    models.CheckConstraint(
                        condition=models.Q(
                            models.Q(("state", "failed"), _negated=True),
                            ("error__isnull", False),
                            _connector="OR",
                        ),
                        name="job_failed_has_error",
                    ),
                ],
            },
        ),
    ]
