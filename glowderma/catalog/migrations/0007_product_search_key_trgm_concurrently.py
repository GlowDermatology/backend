"""catalog.0007 — the trigram GIN index on the apostrophe-free ``search_text`` of ``0006``.

Built ``CONCURRENTLY`` (a non-atomic migration), so product writes are never blocked while it builds; same
name as before (``catalog_product_search_trgm``). If the build fails half way PostgreSQL leaves an INVALID
index: drop it (``DROP INDEX CONCURRENTLY catalog_product_search_trgm``) and migrate again. Reversible.
"""

import django.contrib.postgres.indexes
from django.contrib.postgres.operations import AddIndexConcurrently
from django.db import migrations


class Migration(migrations.Migration):
    atomic = False

    dependencies = [
        ("catalog", "0006_product_search_key"),
    ]

    operations = [
        AddIndexConcurrently(
            model_name="product",
            index=django.contrib.postgres.indexes.GinIndex(
                fields=["search_text"], name="catalog_product_search_trgm", opclasses=["gin_trgm_ops"]
            ),
        ),
    ]
