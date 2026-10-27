"""Append-only trigger on ``integrations_webhookinbox`` (design 01 §2.1).

Depends on core.0008 so the shared ``glowderma_forbid_mutation()`` body this migration re-creates
already carries the purge-window GUC (``core.db.append_only_trigger_sql`` is the single source of it).
"""

from django.db import migrations

from glowderma.core.db import append_only_trigger_sql


class Migration(migrations.Migration):
    dependencies = [
        ("integrations", "0001_initial"),
        ("core", "0008_forbid_mutation_purge_guc"),
    ]
    operations = [migrations.RunSQL(*append_only_trigger_sql("integrations_webhookinbox"))]
