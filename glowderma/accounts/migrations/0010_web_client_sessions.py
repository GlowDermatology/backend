"""B-W1: customer sessions keep their user agent, last IP and GeoIP location; `Device.platform` gets `web`."""

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("accounts", "0009_merge_customer_fold_staff_manager"),
    ]

    operations = [
        migrations.AddField(
            model_name="authsession",
            name="last_ip",
            field=models.GenericIPAddressField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="authsession",
            name="location",
            field=models.CharField(blank=True, db_default="", default="", max_length=120),
        ),
        migrations.AddField(
            model_name="authsession",
            name="user_agent",
            field=models.CharField(blank=True, db_default="", default="", max_length=512),
        ),
        migrations.AlterField(
            model_name="device",
            name="platform",
            field=models.CharField(
                choices=[("ios", "Ios"), ("android", "Android"), ("web", "Web")],
                max_length=8,
            ),
        ),
    ]
