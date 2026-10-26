"""``StaffUser.is_manager`` (admin contract §2.16, decision GST-1): only a manager manages staff.

The data step gives an existing install its first managers so nobody is locked out of staff management:
every superuser (``createstaff --superuser``) becomes a manager, and when no active manager results, the
active staff member with the lowest id (the first account created, normally the owner's) does. It only ever
sets the flag, so a rerun changes nothing; the reverse drops the column (the data step has nothing to undo).
"""

from typing import Any

from django.db import migrations, models


def promote_first_managers(apps: Any, schema_editor: Any) -> None:
    staff_user = apps.get_model("accounts", "StaffUser")
    staff_user.objects.filter(is_superuser=True, is_manager=False).update(is_manager=True)
    if staff_user.objects.filter(is_active=True, is_manager=True).exists():
        return
    first = staff_user.objects.filter(is_active=True).order_by("id").values_list("id", flat=True).first()
    if first is not None:
        staff_user.objects.filter(pk=first).update(is_manager=True)


class Migration(migrations.Migration):
    dependencies = [
        ("accounts", "0007_session_auth_method_identity_client"),
    ]

    operations = [
        migrations.AddField(
            model_name="staffuser",
            name="is_manager",
            field=models.BooleanField(default=False),
        ),
        migrations.RunPython(promote_first_managers, migrations.RunPython.noop, elidable=False),
    ]
