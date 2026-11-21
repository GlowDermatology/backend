"""Every specialist profile is owned by one workforce account for its lifetime.

Pre-production: a profile without an account (seed data from before this change) gets an inactive placeholder
Specialist account so the column can become ``NOT NULL``; staff can change its login email and invite it.
"""

import django.db.models.deletion
from django.db import migrations, models


def give_orphans_an_account(apps, schema_editor):  # type: ignore[no-untyped-def]
    professional_model = apps.get_model("professionals", "Professional")
    staff_model = apps.get_model("accounts", "StaffUser")
    for profile in professional_model.objects.filter(staff_user__isnull=True):
        account = staff_model.objects.create(
            email=f"specialist-{profile.pk}@placeholder.invalid",
            full_name=profile.name[:150],
            role="specialist",
            is_active=False,
            password="!",  # noqa: S106 - an unusable password
        )
        profile.staff_user_id = account.pk
        profile.save(update_fields=["staff_user"])
    staff_model.objects.filter(
        role="specialist", pk__in=professional_model.objects.values("staff_user_id")
    ).update(is_manager=False)


class Migration(migrations.Migration):
    dependencies = [
        ("accounts", "0016_specialist_role"),
        ("professionals", "0008_clinic_assignments"),
    ]

    operations = [
        migrations.RunPython(give_orphans_an_account, migrations.RunPython.noop),
        migrations.AlterField(
            model_name="professional",
            name="staff_user",
            field=models.OneToOneField(
                on_delete=django.db.models.deletion.PROTECT,
                related_name="specialist_profile",
                to="accounts.staffuser",
                verbose_name="xodim hisobi",
            ),
        ),
        migrations.AlterField(
            model_name="professional",
            name="location",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="professionals",
                to="professionals.location",
                verbose_name="filial",
            ),
        ),
        migrations.AlterField(
            model_name="professional",
            name="role",
            field=models.CharField(
                blank=True,
                choices=[("dermatologist", "Dermatolog"), ("cosmetologist", "Kosmetolog")],
                default="",
                max_length=16,
                verbose_name="rol",
            ),
        ),
        migrations.AlterField(
            model_name="professional",
            name="is_active",
            field=models.BooleanField(default=False, verbose_name="e’lon qilingan"),
        ),
    ]
