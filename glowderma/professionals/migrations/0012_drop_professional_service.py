"""Per-specialist services, step 3 of 3 (schema): every service now has its owner and price (``0011``).

The link model ``ProfessionalService`` is deleted first, so ``Service.professional`` can take its reverse
accessor ``Professional.services``; then the owner and the price become required, the price gets its CHECK,
a name is unique within one specialist and the owner's active services are indexed by position.
"""

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("professionals", "0011_split_shared_services")]

    operations = [
        migrations.DeleteModel(name="ProfessionalService"),
        migrations.AlterModelOptions(
            name="service",
            options={
                "ordering": ["professional_id", "position", "id"],
                "verbose_name": "xizmat",
                "verbose_name_plural": "xizmatlar",
            },
        ),
        migrations.AlterField(
            model_name="service",
            name="duration_min",
            field=models.PositiveSmallIntegerField(verbose_name="davomiyligi (daq.)"),
        ),
        migrations.AlterField(
            model_name="service",
            name="price",
            field=models.BigIntegerField(verbose_name="narx"),
        ),
        migrations.AlterField(
            model_name="service",
            name="professional",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.CASCADE,
                related_name="services",
                to="professionals.professional",
                verbose_name="mutaxassis",
            ),
        ),
        migrations.AddIndex(
            model_name="service",
            index=models.Index(
                fields=["professional", "is_active", "position"], name="prof_service_owner_pos"
            ),
        ),
        migrations.AddConstraint(
            model_name="service",
            constraint=models.CheckConstraint(
                condition=models.Q(("price__gte", 0)), name="service_price_non_negative"
            ),
        ),
        migrations.AddConstraint(
            model_name="service",
            constraint=models.UniqueConstraint(
                fields=("professional", "name_uz"), name="service_professional_name_unique"
            ),
        ),
    ]
