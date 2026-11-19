from typing import Any

from django.db import migrations


def seed(apps: Any, schema_editor: Any) -> None:
    from glowderma.assistant import defaults

    defaults.ensure_rows(apps, schema_editor.connection.alias)


class Migration(migrations.Migration):
    dependencies = [("assistant", "0001_initial")]

    operations = [migrations.RunPython(seed, migrations.RunPython.noop)]
