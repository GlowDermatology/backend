"""One ``IntegrationState`` row per provider so reads never write (design 01 §2.3)."""

from django.db import migrations
from django.db.backends.base.schema import BaseDatabaseSchemaEditor
from django.db.migrations.state import StateApps

PROVIDERS = ("billz", "bts", "click", "payme", "eskiz", "fcm")


def create_provider_rows(apps: StateApps, schema_editor: BaseDatabaseSchemaEditor) -> None:
    IntegrationState = apps.get_model("integrations", "IntegrationState")
    for provider in PROVIDERS:
        IntegrationState.objects.get_or_create(provider=provider)


def delete_provider_rows(apps: StateApps, schema_editor: BaseDatabaseSchemaEditor) -> None:
    IntegrationState = apps.get_model("integrations", "IntegrationState")
    IntegrationState.objects.filter(provider__in=PROVIDERS).delete()


class Migration(migrations.Migration):
    dependencies = [("integrations", "0002_webhookinbox_append_only")]
    operations = [migrations.RunPython(create_provider_rows, delete_provider_rows)]
