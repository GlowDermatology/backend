"""``PosDevice.key_prefix`` becomes unique: the prefix names the device for ``revokeposkey``/``rotateposkey``.

A database that already holds two devices on one prefix stops here with their ids instead of failing inside
the index build; fix them by hand (revoke one and create a new key), then migrate again.
"""

from typing import Any

from django.db import migrations, models
from django.db.models import Count


def refuse_shared_prefixes(apps: Any, schema_editor: Any) -> None:
    device_model = apps.get_model("loyalty", "PosDevice")
    shared = list(
        device_model.objects.values("key_prefix")
        .annotate(n=Count("id"))
        .filter(n__gt=1)
        .values_list("key_prefix", flat=True)
    )
    if shared:
        ids = list(
            device_model.objects.filter(key_prefix__in=shared).order_by("pk").values_list("pk", flat=True)
        )
        raise RuntimeError(f"POS devices {ids} share key prefixes; give each its own key before migrating")


class Migration(migrations.Migration):
    dependencies = [("loyalty", "0002_append_only")]

    operations = [
        migrations.RunPython(refuse_shared_prefixes, migrations.RunPython.noop, elidable=True),
        migrations.AlterField(
            model_name="posdevice",
            name="key_prefix",
            field=models.CharField(max_length=12, unique=True),
        ),
    ]
