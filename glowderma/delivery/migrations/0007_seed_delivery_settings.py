"""Insert the ``DeliverySettings`` singleton (pk 1) so a read never writes (``DeliverySettings.load``).

Every method starts disabled and the warehouse empty: nothing is offered until staff configure it. Reverse is
a no-op: 0006's reverse drops the table, and the row is configuration, never demo data.
"""

from django.db import migrations
from django.db.backends.base.schema import BaseDatabaseSchemaEditor
from django.db.migrations.state import StateApps


def create_singleton(apps: StateApps, schema_editor: BaseDatabaseSchemaEditor) -> None:
    DeliverySettings = apps.get_model("delivery", "DeliverySettings")
    DeliverySettings.objects.get_or_create(pk=1)


class Migration(migrations.Migration):
    dependencies = [("delivery", "0006_yandex_delivery")]
    operations = [migrations.RunPython(create_singleton, migrations.RunPython.noop)]
