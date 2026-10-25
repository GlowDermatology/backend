"""Append-only trigger on ``commerce_orderevent`` (design 03 §0.2 / §9 row 11).

Depends on core.0008 so the shared ``glowderma_forbid_mutation()`` body this migration re-creates already
carries the purge-window GUC (``core.db.append_only_trigger_sql`` is the single source of it). Order events
are not purgeable: the GUC only ever opens ``core_auditentry`` and ``integrations_webhookinbox``.
"""

from django.db import migrations

from glowderma.core.db import append_only_trigger_sql


class Migration(migrations.Migration):
    dependencies = [
        ("commerce", "0002_order_sequence"),
        ("core", "0008_forbid_mutation_purge_guc"),
    ]
    operations = [migrations.RunSQL(*append_only_trigger_sql("commerce_orderevent"))]
