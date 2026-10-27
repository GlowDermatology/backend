"""Append-only triggers on ``loyalty_cashbackpolicy``, ``loyalty_cashbacktier`` and ``loyalty_ledgerentry``
(design 02 §0.2 / 03 §9 row 12).

Depends on core.0008 so the shared ``glowderma_forbid_mutation()`` body this migration re-creates already
carries the purge-window GUC (``core.db.append_only_trigger_sql`` is the single source of it). None of the
three tables is purgeable: the GUC only ever opens ``core_auditentry`` and ``integrations_webhookinbox``.
The reverse drops only the three triggers.
"""

from django.db import migrations

from glowderma.core.db import append_only_trigger_sql

APPEND_ONLY_TABLES = ("loyalty_cashbackpolicy", "loyalty_cashbacktier", "loyalty_ledgerentry")


class Migration(migrations.Migration):
    dependencies = [
        ("loyalty", "0001_initial"),
        ("core", "0008_forbid_mutation_purge_guc"),
    ]
    operations = [migrations.RunSQL(*append_only_trigger_sql(table)) for table in APPEND_ONLY_TABLES]
