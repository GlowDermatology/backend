"""The four ``StatusTemplate`` rows with the design texts (design 03 §3.1), so
``GET notification-templates/{status}/`` never 404s for a valid status. Reverse deletes the four rows.

``notifications.apps.ensure_status_templates`` re-ensures them on ``post_migrate`` (after a flush).
"""

from django.db import migrations
from django.db.backends.base.schema import BaseDatabaseSchemaEditor
from django.db.migrations.state import StateApps

from glowderma.notifications.apps import STATUS_TEMPLATES


def create_templates(apps: StateApps, schema_editor: BaseDatabaseSchemaEditor) -> None:
    StatusTemplate = apps.get_model("notifications", "StatusTemplate")
    for status, text_uz, text_ru, text_en in STATUS_TEMPLATES:
        StatusTemplate.objects.get_or_create(
            status=status, defaults={"text_uz": text_uz, "text_ru": text_ru, "text_en": text_en}
        )


def delete_templates(apps: StateApps, schema_editor: BaseDatabaseSchemaEditor) -> None:
    StatusTemplate = apps.get_model("notifications", "StatusTemplate")
    StatusTemplate.objects.filter(status__in=[row[0] for row in STATUS_TEMPLATES]).delete()


class Migration(migrations.Migration):
    dependencies = [("notifications", "0001_initial")]
    operations = [migrations.RunPython(create_templates, delete_templates)]
