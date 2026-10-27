"""Teach the shared append-only trigger function about the retention purge window (decision T6).

DELETE is allowed only when the transaction set ``glowderma.allow_purge = on`` (``core.db.purge_window``) and
only on ``core_auditentry`` / ``integrations_webhookinbox``; every other mutation keeps raising.
"""

from django.db import migrations

from glowderma.core.db import FORBID_MUTATION_FUNCTION, LEGACY_FORBID_MUTATION_FUNCTION


class Migration(migrations.Migration):
    dependencies = [("core", "0007_db_extensions")]
    operations = [
        migrations.RunSQL(sql=FORBID_MUTATION_FUNCTION, reverse_sql=LEGACY_FORBID_MUTATION_FUNCTION)
    ]
