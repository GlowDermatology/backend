"""Append-only trigger on ``delivery_shipmentevent`` (design 02 §0.2 / 03 §9 row 14).

Depends on core.0008 so the shared ``glowderma_forbid_mutation()`` body this migration re-creates already
carries the purge-window GUC (``core.db.append_only_trigger_sql`` is the single source of it). Shipment events
are never purged (D-4): the GUC only ever opens ``core_auditentry`` and ``integrations_webhookinbox``.
"""

from django.db import migrations

from glowderma.core.db import append_only_trigger_sql


class Migration(migrations.Migration):
    dependencies = [
        ("delivery", "0001_initial"),
        ("core", "0008_forbid_mutation_purge_guc"),
    ]
    operations = [migrations.RunSQL(*append_only_trigger_sql("delivery_shipmentevent"))]
