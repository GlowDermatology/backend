"""notifications.0011 — the campaign approval workflow, constraints step: the rebuilt
``campaign_state_fields`` CHECK (one branch per state; legacy ``sent`` / ``cancelled`` rows pass without
approval fields) and the partial index the beat reads due approved campaigns from (``dispatch_after`` where
``state = 'scheduled'``), applied after ``0010`` rewrote the old states."""

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("notifications", "0010_campaign_approval_data"),
    ]

    operations = [
        migrations.AddIndex(
            model_name="campaign",
            index=models.Index(
                condition=models.Q(("state", "scheduled")),
                fields=["dispatch_after"],
                name="campaign_dispatch_idx",
            ),
        ),
        migrations.AddConstraint(
            model_name="campaign",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    models.Q(
                        ("state", "draft"),
                        ("failed__isnull", True),
                        ("failed_at__isnull", True),
                        ("sent__isnull", True),
                        ("sent_at__isnull", True),
                        ("started_at__isnull", True),
                        ("approved_at__isnull", True),
                        ("approved_revision__isnull", True),
                        ("dispatch_after__isnull", True),
                        ("stop_requested_at__isnull", True),
                        ("stopped_at__isnull", True),
                    ),
                    models.Q(
                        ("state", "tested"),
                        ("test_confirmed_at__isnull", False),
                        ("tested_revision", models.F("content_revision")),
                        ("failed__isnull", True),
                        ("failed_at__isnull", True),
                        ("sent__isnull", True),
                        ("sent_at__isnull", True),
                        ("started_at__isnull", True),
                        ("approved_at__isnull", True),
                        ("approved_revision__isnull", True),
                        ("dispatch_after__isnull", True),
                        ("stop_requested_at__isnull", True),
                        ("stopped_at__isnull", True),
                    ),
                    models.Q(
                        ("approved_at__isnull", False),
                        ("approved_revision__isnull", False),
                        ("dispatch_after__isnull", False),
                        ("state", "scheduled"),
                        ("failed__isnull", True),
                        ("failed_at__isnull", True),
                        ("sent__isnull", True),
                        ("sent_at__isnull", True),
                        ("started_at__isnull", True),
                        ("stop_requested_at__isnull", True),
                        ("stopped_at__isnull", True),
                    ),
                    models.Q(
                        ("failed_at__isnull", True),
                        ("sent_at__isnull", True),
                        ("started_at__isnull", False),
                        ("state", "sending"),
                        ("stop_requested_at__isnull", True),
                        ("stopped_at__isnull", True),
                    ),
                    models.Q(
                        ("failed_at__isnull", True),
                        ("sent_at__isnull", True),
                        ("started_at__isnull", False),
                        ("state", "stopping"),
                        ("stop_requested_at__isnull", False),
                        ("stopped_at__isnull", True),
                    ),
                    models.Q(
                        ("failed__isnull", False),
                        ("failed_at__isnull", True),
                        ("sent__isnull", False),
                        ("sent_at__isnull", False),
                        ("started_at__isnull", False),
                        ("state", "sent"),
                        ("stopped_at__isnull", True),
                    ),
                    models.Q(
                        ("failed__isnull", False),
                        ("sent__isnull", False),
                        ("sent_at__isnull", True),
                        ("started_at__isnull", False),
                        ("state", "partially_sent"),
                        models.Q(
                            ("stopped_at__isnull", False),
                            ("failed_at__isnull", False),
                            _connector="OR",
                        ),
                    ),
                    models.Q(
                        ("failed__isnull", False),
                        ("failed_at__isnull", False),
                        ("sent__isnull", False),
                        ("sent_at__isnull", True),
                        ("started_at__isnull", False),
                        ("state", "failed"),
                    ),
                    models.Q(
                        ("failed_at__isnull", True),
                        ("sent_at__isnull", True),
                        ("state", "cancelled"),
                    ),
                    _connector="OR",
                ),
                name="campaign_state_fields",
            ),
        ),
    ]
