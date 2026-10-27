"""professionals.0002 — the FND-7 fields: ``Professional.qualifications_{uz,ru,en}``, the optional linked
staff account ``Professional.staff_user`` and ``Service.category``.

Existing services get a category inferred from who offers them: a service linked only to cosmetologists is
``cosmetology``, every other one keeps the column default ``dermatology`` (staff correct it in the admin API).
The inference is deterministic, so running it again changes nothing; reversing drops the columns.

The new NOT NULL columns carry a database default (``db_default``): during a rollout the previous release
still inserts specialists and services without them, which must not fail.
"""

from typing import Any

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models

DERMATOLOGIST, COSMETOLOGIST = "dermatologist", "cosmetologist"


def infer_categories(apps: Any, schema_editor: Any) -> None:
    Service = apps.get_model("professionals", "Service")
    ProfessionalService = apps.get_model("professionals", "ProfessionalService")
    links = ProfessionalService.objects.values_list("service_id", "professional__role")
    cosmetic = {service_id for service_id, role in links if role == COSMETOLOGIST}
    dermatologic = {service_id for service_id, role in links if role == DERMATOLOGIST}
    Service.objects.filter(pk__in=cosmetic - dermatologic).update(category="cosmetology")


class Migration(migrations.Migration):
    # accounts.0001 holds StaffUser (as in 0001_initial: never a later accounts migration).
    dependencies = [("professionals", "0001_initial"), ("accounts", "0001_initial")]

    operations = [
        migrations.AddField(
            model_name="professional",
            name="qualifications_en",
            field=models.TextField(blank=True, default="", db_default=""),
        ),
        migrations.AddField(
            model_name="professional",
            name="qualifications_ru",
            field=models.TextField(blank=True, default="", db_default=""),
        ),
        migrations.AddField(
            model_name="professional",
            name="qualifications_uz",
            field=models.TextField(blank=True, default="", db_default=""),
        ),
        migrations.AddField(
            model_name="professional",
            name="staff_user",
            field=models.OneToOneField(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="+",
                to=settings.AUTH_USER_MODEL,
            ),
        ),
        migrations.AddField(
            model_name="service",
            name="category",
            field=models.CharField(
                choices=[("dermatology", "Dermatology"), ("cosmetology", "Cosmetology")],
                db_default="dermatology",
                default="dermatology",
                max_length=16,
            ),
        ),
        migrations.RunPython(infer_categories, migrations.RunPython.noop),
    ]
