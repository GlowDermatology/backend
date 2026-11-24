"""Uzum joins the payment provider choices (and its refund manual reason): state only, no column changes."""

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("payments", "0007_uz_field_labels"),
    ]

    operations = [
        migrations.AlterField(
            model_name="payment",
            name="provider",
            field=models.CharField(
                choices=[("click", "Click"), ("payme", "Payme"), ("uzum", "Uzum")],
                max_length=16,
                verbose_name="provayder",
            ),
        ),
        migrations.AlterField(
            model_name="refund",
            name="manual_reason",
            field=models.CharField(
                blank=True,
                choices=[
                    ("payme", "Payme kabinetida"),
                    ("no_paydoc", "Click paydoc yo‘q"),
                    ("fake_mode", "Click sinov rejimida"),
                    ("uzum_fake", "Uzum sinov rejimida"),
                ],
                max_length=16,
                verbose_name="qo‘lda kiritish sababi",
            ),
        ),
    ]
