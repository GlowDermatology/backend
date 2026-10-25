"""Insert the settings singleton (pk 1) so ``GET settings/`` never writes (design 01 §3.1).

Reverse is a no-op: 0001's reverse drops the table, and the row is configuration, never demo data.
"""

from django.db import migrations
from django.db.backends.base.schema import BaseDatabaseSchemaEditor
from django.db.migrations.state import StateApps


def create_singleton(apps: StateApps, schema_editor: BaseDatabaseSchemaEditor) -> None:
    SiteSettings = apps.get_model("site_config", "SiteSettings")
    SiteSettings.objects.get_or_create(pk=1)


class Migration(migrations.Migration):
    dependencies = [("site_config", "0001_initial")]
    operations = [migrations.RunPython(create_singleton, migrations.RunPython.noop)]
