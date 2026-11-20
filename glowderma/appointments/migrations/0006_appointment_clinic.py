"""Persist each existing appointment's previously displayed primary clinic."""

import django.db.models.deletion
from django.apps.registry import Apps
from django.db import migrations, models
from django.db.backends.base.schema import BaseDatabaseSchemaEditor


def backfill_appointment_clinics(apps: Apps, schema_editor: BaseDatabaseSchemaEditor) -> None:
    professional = apps.get_model("professionals", "Professional")
    appointments = apps.get_model("appointments", "Appointment")
    database = schema_editor.connection.alias
    primary = models.Subquery(
        professional.objects.using(database)
        .filter(pk=models.OuterRef("professional_id"))
        .values("location_id")
    )
    appointments.objects.using(database).update(location_id=primary)


class Migration(migrations.Migration):
    dependencies = [
        ("appointments", "0005_uz_field_labels"),
        ("professionals", "0008_clinic_assignments"),
    ]

    operations = [
        migrations.AddField(
            model_name="appointment",
            name="location",
            field=models.ForeignKey(
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="appointments",
                to="professionals.location",
                verbose_name="filial",
            ),
        ),
        migrations.RunPython(backfill_appointment_clinics, migrations.RunPython.noop),
        # Flush PostgreSQL's deferred foreign-key trigger checks before altering the table.
        migrations.RunSQL("SET CONSTRAINTS ALL IMMEDIATE", reverse_sql=migrations.RunSQL.noop),
        migrations.AlterField(
            model_name="appointment",
            name="location",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.PROTECT,
                related_name="appointments",
                to="professionals.location",
                verbose_name="filial",
            ),
        ),
    ]
