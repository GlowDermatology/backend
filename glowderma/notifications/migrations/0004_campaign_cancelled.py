"""notifications.0004 — a scheduled campaign can be cancelled (M3, admin contract §2.8
``POST notifications/{id}/cancel/``): state ``cancelled`` with ``cancelled_at``. The state check is rebuilt
with the new branch (a cancelled campaign never started: no counters); every existing row satisfies both
constraints (``cancelled_at`` is null)."""

from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("accounts", "0007_session_auth_method_identity_client"),
        ("catalog", "0001_initial"),
        ("notifications", "0003_inbox_account_source"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.RemoveConstraint(
            model_name="campaign",
            name="campaign_state_fields",
        ),
        migrations.AddField(
            model_name="campaign",
            name="cancelled_at",
            field=models.DateTimeField(null=True),
        ),
        migrations.AlterField(
            model_name="campaign",
            name="state",
            field=models.CharField(
                choices=[
                    ("scheduled", "Scheduled"),
                    ("queued", "Queued"),
                    ("sending", "Sending"),
                    ("sent", "Sent"),
                    ("cancelled", "Cancelled"),
                ],
                default="queued",
                max_length=16,
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
                    models.Q(
                        ("failed__isnull", True),
                        ("scheduled_at__isnull", False),
                        ("sent__isnull", True),
                        ("sent_at__isnull", True),
                        ("started_at__isnull", True),
                        ("state", "cancelled"),
                    ),
                    _connector="OR",
                ),
                name="campaign_state_fields",
            ),
        ),
        migrations.AddConstraint(
            model_name="campaign",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    models.Q(("cancelled_at__isnull", False), ("state", "cancelled")),
                    models.Q(
                        models.Q(("state", "cancelled"), _negated=True),
                        ("cancelled_at__isnull", True),
                    ),
                    _connector="OR",
                ),
                name="campaign_cancelled_at",
            ),
        ),
    ]
