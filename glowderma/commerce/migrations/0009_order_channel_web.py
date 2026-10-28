"""B-W1: `Order.channel` gains `web` (orders placed on the customer website)."""

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("commerce", "0008_backfill_refund_manual_reason"),
    ]

    operations = [
        migrations.AlterField(
            model_name="order",
            name="channel",
            field=models.CharField(
                choices=[("ios", "Ios"), ("android", "Android"), ("web", "Web")],
                max_length=8,
            ),
        ),
    ]
