"""payments.0003 — partial index of the late captures whose money is not back yet (consistency report K5).

``error_code = 'late_capture' AND status = 'paid'`` holds a handful of rows at most, so K5 never scans the
payments table, which grows with every order. Built ``CONCURRENTLY`` (a non-atomic migration) so a rolling
deploy never blocks payment writes.
"""

from django.contrib.postgres.operations import AddIndexConcurrently
from django.db import migrations, models


class Migration(migrations.Migration):
    atomic = False

    dependencies = [("payments", "0002_payment_active_excludes_late_capture")]

    operations = [
        AddIndexConcurrently(
            model_name="payment",
            index=models.Index(
                condition=models.Q(("error_code", "late_capture"), ("status", "paid")),
                fields=["confirmed_at"],
                name="payment_late_capture_open_idx",
            ),
        ),
    ]
