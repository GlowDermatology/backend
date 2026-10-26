"""core.0010 — partial index of the outbox rows that are not done (docs/ops/monitoring.md, consistency K1/K6).

Built ``CONCURRENTLY`` (a non-atomic migration): the outbox takes writes from every request that enqueues a
message, and a plain ``CREATE INDEX`` would block them while it scans the table during a rolling deploy.
"""

from django.contrib.postgres.operations import AddIndexConcurrently
from django.db import migrations, models


class Migration(migrations.Migration):
    atomic = False

    dependencies = [
        ("core", "0009_outboxmessage_deferred_since"),
    ]

    operations = [
        AddIndexConcurrently(
            model_name="outboxmessage",
            index=models.Index(
                condition=models.Q(("status", "done"), _negated=True),
                fields=["status", "available_at"],
                name="outbox_unfinished_idx",
            ),
        ),
    ]
