"""Per-specialist services, step 1 of 3 (schema): every service gets an owner specialist and its own price.

``Service.professional`` is added nullable (``0011`` fills it), ``default_duration_min`` becomes
``duration_min`` and ``default_price`` becomes ``price`` (still nullable until ``0012``). The temporary
``related_name="+"`` keeps the reverse accessor ``Professional.services`` free for the old link model, which
``0012`` deletes.
"""

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("professionals", "0009_owned_specialist_profiles")]

    operations = [
        migrations.AddField(
            model_name="service",
            name="professional",
            field=models.ForeignKey(
                null=True,
                on_delete=django.db.models.deletion.CASCADE,
                related_name="+",
                to="professionals.professional",
                verbose_name="mutaxassis",
            ),
        ),
        migrations.RemoveConstraint(model_name="service", name="service_duration_5_480"),
        migrations.RenameField(
            model_name="service", old_name="default_duration_min", new_name="duration_min"
        ),
        migrations.AddConstraint(
            model_name="service",
            constraint=models.CheckConstraint(
                condition=models.Q(("duration_min__gte", 5), ("duration_min__lte", 480)),
                name="service_duration_5_480",
            ),
        ),
        migrations.RemoveConstraint(model_name="service", name="service_default_price_non_negative"),
        migrations.RenameField(model_name="service", old_name="default_price", new_name="price"),
    ]
