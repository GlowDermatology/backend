from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("loyalty", "0003_posdevice_key_prefix_unique"),
    ]

    operations = [
        migrations.AddField(
            model_name="purchase",
            name="billz_payment_attempted_at",
            field=models.DateTimeField(null=True),
        ),
    ]
