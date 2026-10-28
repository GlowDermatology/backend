"""catalog.0006 — ``Product.search_text`` without apostrophes (customer catalog search).

The search key (``core.search.SearchKey``) is the GC-1 fold with every apostrophe encoding removed instead
of unified, so ``kopik`` finds ``ko‘pik``. As in ``0002``, PostgreSQL 16 cannot change a stored generated
column's expression in place: the column (and its trigram index) is dropped and added again, which
recomputes every row (the product table is small: the rewrite holds its lock well under a second); ``0007``
rebuilds the index ``CONCURRENTLY``. Reversible: the reverse restores the folded column of ``0002``.
"""

from django.db import migrations, models

import glowderma.catalog.models
import glowderma.core.search


class Migration(migrations.Migration):
    dependencies = [
        ("catalog", "0005_seo_slugs"),
    ]

    operations = [
        migrations.RemoveIndex(model_name="product", name="catalog_product_search_trgm"),
        migrations.RemoveField(model_name="product", name="search_text"),
        migrations.AddField(
            model_name="product",
            name="search_text",
            field=models.GeneratedField(
                db_persist=True,
                expression=glowderma.core.search.SearchKey(
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
