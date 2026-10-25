"""Global database objects every business module relies on (created once, in the lowest layer).

- ``pg_trgm`` — trigram GIN indexes on ``accounts.Customer.name`` and ``catalog.Product.search_text``.
- ``btree_gist`` — the appointment overlap exclusion constraint.
- ``glowderma_ci`` — ICU case-insensitive, accent-sensitive collation used only in ``ORDER BY`` (never for
  equality, LIKE, DISTINCT, unique constraints or a column's ``db_collation``).

Both extensions are trusted (PostgreSQL ≥ 13): a role with CREATE on the database installs them without
superuser rights. Reversal drops them, which is only meaningful on a scratch database.
"""

from django.contrib.postgres.operations import BtreeGistExtension, TrigramExtension
from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [("core", "0006_outbox_claim_token")]
    operations = [
        TrigramExtension(),
        BtreeGistExtension(),
        migrations.RunSQL(
            sql=(
                "CREATE COLLATION IF NOT EXISTS glowderma_ci "
                "(provider = icu, locale = 'und-u-ks-level2', deterministic = false);"
            ),
            reverse_sql="DROP COLLATION IF EXISTS glowderma_ci;",
        ),
    ]
