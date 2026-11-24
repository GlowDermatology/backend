"""SMS codes go through DevSMS instead of Eskiz: the ``eskiz`` ``IntegrationState`` row becomes ``devsms``
(its last success and failure facts describe the SMS channel; ``apps.ensure_provider_rows`` inserts the row
when it is missing). The choices change is state only: no column changes."""

from django.db import migrations, models
from django.db.backends.base.schema import BaseDatabaseSchemaEditor
from django.db.migrations.state import StateApps


def eskiz_to_devsms(apps: StateApps, schema_editor: BaseDatabaseSchemaEditor) -> None:
    IntegrationState = apps.get_model("integrations", "IntegrationState")
    if IntegrationState.objects.filter(provider="devsms").exists():
        IntegrationState.objects.filter(provider="eskiz").delete()
    else:
        IntegrationState.objects.filter(provider="eskiz").update(provider="devsms")
    IntegrationState.objects.get_or_create(provider="devsms")


def devsms_to_eskiz(apps: StateApps, schema_editor: BaseDatabaseSchemaEditor) -> None:
    IntegrationState = apps.get_model("integrations", "IntegrationState")
    if IntegrationState.objects.filter(provider="eskiz").exists():
        IntegrationState.objects.filter(provider="devsms").delete()
    else:
        IntegrationState.objects.filter(provider="devsms").update(provider="eskiz")


class Migration(migrations.Migration):
    dependencies = [
        ("integrations", "0006_yandex_provider"),
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
                ],
                max_length=16,
                unique=True,
                verbose_name="provayder",
            ),
        ),
        migrations.RunPython(eskiz_to_devsms, devsms_to_eskiz),
    ]
