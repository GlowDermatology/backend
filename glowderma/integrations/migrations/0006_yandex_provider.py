"""Yandex Delivery joins both provider sets, and its ``IntegrationState`` row is inserted (reads never write;
``apps.ensure_provider_rows`` re-inserts it after a flush)."""

from django.db import migrations, models
from django.db.backends.base.schema import BaseDatabaseSchemaEditor
from django.db.migrations.state import StateApps

PROVIDER = "yandex"


def create_yandex_row(apps: StateApps, schema_editor: BaseDatabaseSchemaEditor) -> None:
    IntegrationState = apps.get_model("integrations", "IntegrationState")
    IntegrationState.objects.get_or_create(provider=PROVIDER)


def delete_yandex_row(apps: StateApps, schema_editor: BaseDatabaseSchemaEditor) -> None:
    IntegrationState = apps.get_model("integrations", "IntegrationState")
    IntegrationState.objects.filter(provider=PROVIDER).delete()


class Migration(migrations.Migration):
    dependencies = [
        ("integrations", "0005_uz_choice_labels"),
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
                    ("eskiz", "Eskiz"),
                    ("fcm", "FCM"),
                    ("yandex", "Yandex Delivery"),
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
                ],
                max_length=16,
                verbose_name="provayder",
            ),
        ),
        migrations.RunPython(create_yandex_row, delete_yandex_row),
    ]
