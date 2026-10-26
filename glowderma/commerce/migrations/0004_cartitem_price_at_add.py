"""``CartItem.price_at_add`` (customer contract §2.5, M2-C12): the price at the last cart write, so the cart can
say "the price changed since you added it". Lines created before this migration keep ``null``
(``price_changed: false``)."""

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("commerce", "0003_orderevent_append_only")]

    operations = [
        migrations.AddField(
            model_name="cartitem", name="price_at_add", field=models.BigIntegerField(null=True)
        ),
    ]
