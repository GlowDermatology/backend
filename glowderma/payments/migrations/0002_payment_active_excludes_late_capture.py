"""``payment_one_active_per_order`` ignores late captures (customer contract §2.13.0, plan §3 P2).

A refused capture (``error_code = 'late_capture'``) is ``paid`` until its refund succeeds; a double payment
must be able to sit next to the order's real paid payment, so the partial unique covers only
``status IN ('pending', 'paid') AND error_code <> 'late_capture'``.
"""

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("payments", "0001_initial")]

    operations = [
        migrations.RemoveConstraint(model_name="payment", name="payment_one_active_per_order"),
        migrations.AddConstraint(
            model_name="payment",
            constraint=models.UniqueConstraint(
                condition=models.Q(("status__in", ["pending", "paid"]))
                & ~models.Q(("error_code", "late_capture")),
                fields=("order",),
                name="payment_one_active_per_order",
            ),
        ),
    ]
