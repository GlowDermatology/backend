from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0008_forbid_mutation_purge_guc"),
    ]

    operations = [
        migrations.AddField(
            model_name="outboxmessage",
            name="deferred_since",
            field=models.DateTimeField(null=True),
        ),
    ]
