"""Uzum joins the payment provider choices (and its refund manual reason): state only, no column changes."""

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("commerce", "0013_yandex_delivery"),
    ]

    operations = [
        migrations.AlterField(
            model_name="order",
            name="payment_provider",
            field=models.CharField(
                choices=[("click", "Click"), ("payme", "Payme"), ("uzum", "Uzum")],
                max_length=16,
                verbose_name="to‘lov provayderi",
            ),
        ),
    ]
