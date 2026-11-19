"""Add additional clinic assignments and assign existing working periods to their primary clinic."""

import django.db.models.deletion
from django.apps.registry import Apps
from django.db import migrations, models
from django.db.backends.base.schema import BaseDatabaseSchemaEditor


def backfill_schedule_clinics(apps: Apps, schema_editor: BaseDatabaseSchemaEditor) -> None:
    professional = apps.get_model("professionals", "Professional")
    hours = apps.get_model("professionals", "WorkingHours")
    overrides = apps.get_model("professionals", "WorkingHoursOverride")
    database = schema_editor.connection.alias
    primary = models.Subquery(
        professional.objects.using(database)
        .filter(pk=models.OuterRef("professional_id"))
        .values("location_id")
    )
    hours.objects.using(database).update(location_id=primary)
    overrides.objects.using(database).filter(start__isnull=False).update(location_id=primary)


class Migration(migrations.Migration):
    dependencies = [("professionals", "0007_uz_field_labels")]

    operations = [
        migrations.AddField(
            model_name="location",
            name="directions_uz",
            field=models.CharField(
                blank=True, db_default="", default="", max_length=500, verbose_name="mo‘ljal (uz)"
            ),
        ),
        migrations.AddField(
            model_name="location",
            name="directions_ru",
            field=models.CharField(
                blank=True, db_default="", default="", max_length=500, verbose_name="mo‘ljal (ru)"
            ),
        ),
        migrations.AddField(
            model_name="location",
            name="directions_en",
            field=models.CharField(
                blank=True, db_default="", default="", max_length=500, verbose_name="mo‘ljal (en)"
            ),
        ),
        migrations.AddField(
            model_name="professional",
            name="additional_locations",
            field=models.ManyToManyField(
                blank=True,
                related_name="additional_professionals",
                to="professionals.location",
                verbose_name="boshqa filiallar",
            ),
        ),
        migrations.AddField(
            model_name="workinghours",
            name="location",
            field=models.ForeignKey(
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="+",
                to="professionals.location",
                verbose_name="filial",
            ),
        ),
        migrations.AddField(
            model_name="workinghoursoverride",
            name="location",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="+",
                to="professionals.location",
                verbose_name="filial",
            ),
        ),
        migrations.RunPython(backfill_schedule_clinics, migrations.RunPython.noop),
        # Flush PostgreSQL's deferred foreign-key trigger checks before altering the table.
        migrations.RunSQL("SET CONSTRAINTS ALL IMMEDIATE", reverse_sql=migrations.RunSQL.noop),
        migrations.AlterField(
            model_name="workinghours",
            name="location",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.PROTECT,
                related_name="+",
                to="professionals.location",
                verbose_name="filial",
            ),
        ),
        migrations.AddConstraint(
            model_name="workinghoursoverride",
            constraint=models.CheckConstraint(
                condition=models.Q(start__isnull=True, location__isnull=True)
                | models.Q(start__isnull=False, location__isnull=False),
                name="hoursoverride_location_matches_hours",
            ),
        ),
    ]
