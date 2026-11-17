"""core.0014 — two indexes of the audit table for the admin audit log (admin contract §2.13, feat/admin-ux).

* ``audit_request_id_like_idx``: ``request_id varchar_pattern_ops WHERE NOT request_id = ''`` — the
  ``request_id`` filter is a prefix match (``LIKE 'abc%'``), which a default-collation btree cannot serve;
  rows written outside a request (beat, outbox handlers, management commands) carry ``""`` and are left out.
* ``audit_actor_created_idx``: ``(actor_type, actor_id, created_at)`` — the ``actor`` filter and the
  tiered visibility's "own entries" test, newest first.

Built ``CONCURRENTLY`` (a non-atomic migration): every audited request inserts into this table, and a plain
``CREATE INDEX`` would block those writes while it scans the table during a rolling deploy. Both statements
are valid on PostgreSQL 16 and 18.
"""

from django.contrib.postgres.operations import AddIndexConcurrently
from django.db import migrations, models


class Migration(migrations.Migration):
    atomic = False

    dependencies = [
        ("core", "0013_uz_field_labels"),
    ]

    operations = [
        AddIndexConcurrently(
            model_name="auditentry",
            index=models.Index(
                condition=models.Q(("request_id", ""), _negated=True),
                fields=["request_id"],
                name="audit_request_id_like_idx",
                opclasses=["varchar_pattern_ops"],
            ),
        ),
        AddIndexConcurrently(
            model_name="auditentry",
            index=models.Index(
                fields=["actor_type", "actor_id", "created_at"], name="audit_actor_created_idx"
            ),
        ),
    ]
