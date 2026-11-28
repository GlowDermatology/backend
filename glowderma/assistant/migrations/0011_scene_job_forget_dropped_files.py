"""A reviewed or expired scene job's files were deleted but their names stayed on the row, so
``media_storage_check`` reported them missing. Forget those names (the files are already gone)."""

from django.db import migrations

DROPPED = ("accepted", "discarded", "expired")


def forget_dropped_files(apps, schema_editor):  # type: ignore[no-untyped-def]
    SceneJob = apps.get_model("assistant", "SceneJob")
    SceneJob.objects.filter(outcome__in=DROPPED).exclude(candidate="", overlay="").update(
        candidate="", overlay=""
    )


class Migration(migrations.Migration):
    dependencies = [("assistant", "0010_scene_template_reference")]

    operations = [migrations.RunPython(forget_dropped_files, migrations.RunPython.noop)]
