"""Append-only trigger on ``appointments_appointmentevent`` (design 03 §0.2, §2.2).

Depends on core.0008 so the shared ``glowderma_forbid_mutation()`` body this migration re-creates already
carries the purge-window GUC (``core.db.append_only_trigger_sql`` is the single source of it); appointment
events are not purgeable (T6 names only ``WebhookInbox`` and ``AuditEntry``).
"""

from django.db import migrations

from glowderma.core.db import append_only_trigger_sql


class Migration(migrations.Migration):
    dependencies = [
        ("appointments", "0001_initial"),
        ("core", "0008_forbid_mutation_purge_guc"),
    ]
    operations = [migrations.RunSQL(*append_only_trigger_sql("appointments_appointmentevent"))]
