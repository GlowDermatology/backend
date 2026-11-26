from typing import Any

from django.db import migrations


def seed(apps: Any, schema_editor: Any) -> None:
    from glowderma.assistant import defaults

    defaults.ensure_scene_rows(apps, schema_editor.connection.alias)


class Migration(migrations.Migration):
    dependencies = [("assistant", "0005_scenes")]

    operations = [migrations.RunPython(seed, migrations.RunPython.noop)]
