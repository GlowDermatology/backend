"""catalog.0002 — fold Uzbek apostrophes and ё in ``Product.search_text`` (CRIT-SEARCH-UZ-APOSTROPHE, GC-1).

PostgreSQL 16 cannot change a stored generated column's expression in place (``SET EXPRESSION`` is PG 17+) and
Django refuses to alter a ``GeneratedField``: the column is dropped and added again with the folded expression
(``core.search.SearchFold``), which recomputes it for every row. The trigram index on it goes with the old
column; ``0003`` builds the new one ``CONCURRENTLY`` (outside this transaction). The product table is small
(hundreds to a few thousand rows), so the rewrite holds its lock for well under a second. Reversible: the
reverse drops the folded column and restores the ``lower(…)`` one and its index.
"""

from django.db import migrations, models

import glowderma.catalog.models
import glowderma.core.search


class Migration(migrations.Migration):
    dependencies = [
        ("catalog", "0001_initial"),
    ]

    operations = [
        migrations.RemoveIndex(model_name="product", name="catalog_product_search_trgm"),
        migrations.RemoveField(model_name="product", name="search_text"),
        migrations.AddField(
            model_name="product",
            name="search_text",
            field=models.GeneratedField(
                db_persist=True,
                expression=glowderma.core.search.SearchFold(
                    glowderma.catalog.models.JoinText(
                        models.F("name_uz"),
                        models.Value(" "),
                        models.F("name_ru"),
                        models.Value(" "),
                        models.F("name_en"),
                        models.Value(" "),
                        models.F("billz_name"),
                        models.Value(" "),
                        models.F("billz_id"),
                    )
                ),
                output_field=models.TextField(),
            ),
        ),
    ]
