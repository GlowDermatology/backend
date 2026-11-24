"""Teach the shared append-only trigger function about the ops console's full purge of one customer.

Besides the retention purge window (core.0008), DELETE is now also allowed when the transaction set
``glowderma.allow_customer_purge = on`` (``core.db.customer_purge_window``), and only on
``commerce_orderevent`` / ``appointments_appointmentevent``; every UPDATE, and every DELETE elsewhere, keeps
raising.
"""

from django.db import migrations

from glowderma.core.db import FORBID_MUTATION_FUNCTION, RETENTION_ONLY_FORBID_MUTATION_FUNCTION


class Migration(migrations.Migration):
    dependencies = [("core", "0014_audit_request_id_actor_idx")]
    operations = [
        migrations.RunSQL(sql=FORBID_MUTATION_FUNCTION, reverse_sql=RETENTION_ONLY_FORBID_MUTATION_FUNCTION)
    ]
