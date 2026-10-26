"""``StockReservation.billz_settled_at`` and the partial index of the unsettled converted rows (NEW-2)."""

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("catalog", "0001_initial"),
        ("commerce", "0005_orderitem_image_path"),
    ]

    operations = [
        migrations.AddField(
            model_name="stockreservation",
            name="billz_settled_at",
            field=models.DateTimeField(null=True),
        ),
        migrations.AddIndex(
            model_name="stockreservation",
            index=models.Index(
                condition=models.Q(("billz_settled_at__isnull", True), ("state", "converted")),
                fields=["product"],
                name="reservation_unsettled_prod_idx",
            ),
        ),
    ]
