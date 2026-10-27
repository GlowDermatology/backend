"""payments.0004 — ``Refund.manual_reason`` (M3, admin contract §2.2): why a refund is (or was)
``manual_required`` — ``payme``, ``no_paydoc`` or ``fake_mode``; blank for a refund that was never
manual. Adding a blank string column needs no table rewrite on PostgreSQL; ``commerce.0008`` fills the
existing rows."""

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("payments", "0003_payment_late_capture_open_idx"),
    ]

    operations = [
        migrations.AddField(
            model_name="refund",
            name="manual_reason",
            field=models.CharField(
                blank=True,
                choices=[
                    ("payme", "Payme"),
                    ("no_paydoc", "No Paydoc"),
                    ("fake_mode", "Fake Mode"),
                ],
                max_length=16,
            ),
        ),
    ]
