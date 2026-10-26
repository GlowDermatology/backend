"""loyalty.0005 — partial index of the online purchases whose Billz sale is not recorded (consistency K1).

``source = 'online' AND NOT reversed AND (billz_pushed_at IS NULL OR billz_sale_id = '')`` holds the sales
still to push or to enter by hand — a handful of rows — so the K1 check without a look-back never walks the
purchases of every order ever paid. Built ``CONCURRENTLY`` (a non-atomic migration) so a rolling deploy never
blocks the purchase writes of a payment.
"""

from django.contrib.postgres.operations import AddIndexConcurrently
from django.db import migrations, models


class Migration(migrations.Migration):
    atomic = False

    dependencies = [("loyalty", "0004_purchase_billz_payment_attempted_at")]

    operations = [
        AddIndexConcurrently(
            model_name="purchase",
            index=models.Index(
                condition=models.Q(
                    ("reversed", False),
                    ("source", "online"),
                    models.Q(("billz_pushed_at__isnull", True), ("billz_sale_id", ""), _connector="OR"),
                ),
                fields=["order"],
                name="purchase_unpushed_online_idx",
            ),
        ),
    ]
