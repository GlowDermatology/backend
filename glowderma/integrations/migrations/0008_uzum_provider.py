"""Uzum joins both provider sets, and its ``IntegrationState`` row is inserted (reads never write;
``apps.ensure_provider_rows`` re-inserts it after a flush)."""

from django.db import migrations, models
from django.db.backends.base.schema import BaseDatabaseSchemaEditor
from django.db.migrations.state import StateApps

PROVIDER = "uzum"


def create_uzum_row(apps: StateApps, schema_editor: BaseDatabaseSchemaEditor) -> None:
    IntegrationState = apps.get_model("integrations", "IntegrationState")
    IntegrationState.objects.get_or_create(provider=PROVIDER)


def delete_uzum_row(apps: StateApps, schema_editor: BaseDatabaseSchemaEditor) -> None:
    IntegrationState = apps.get_model("integrations", "IntegrationState")
    IntegrationState.objects.filter(provider=PROVIDER).delete()


class Migration(migrations.Migration):
    dependencies = [
        ("integrations", "0007_devsms_provider"),
    ]

    operations = [
        migrations.AlterField(
            model_name="integrationstate",
            name="provider",
            field=models.CharField(
                choices=[
                    ("billz", "Billz"),
                    ("bts", "BTS"),
                    ("click", "Click"),
                    ("payme", "Payme"),
                    ("devsms", "DevSMS"),
                    ("fcm", "FCM"),
                    ("yandex", "Yandex Delivery"),
                    ("uzum", "Uzum"),
                ],
                max_length=16,
                unique=True,
                verbose_name="provayder",
            ),
        ),
        migrations.AlterField(
            model_name="webhookinbox",
            name="provider",
            field=models.CharField(
                choices=[
                    ("click", "Click"),
                    ("payme", "Payme"),
                    ("bts", "BTS"),
                    ("billz", "Billz"),
                    ("yandex", "Yandex Delivery"),
                    ("uzum", "Uzum"),
                ],
                max_length=16,
                verbose_name="provayder",
            ),
        ),
        migrations.RunPython(create_uzum_row, delete_uzum_row),
    ]
