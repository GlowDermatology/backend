"""B-W1 SEO: ``Product.slug`` (backfilled from the Uzbek name, else the Billz name, unique, never only
digits) and the optional per-language ``seo_title`` / ``seo_description`` of products and categories."""

from typing import Any

from django.conf import settings
from django.db import migrations, models

from glowderma.core import slugs

SLUG_MAX_LENGTH = 140


def backfill_product_slugs(apps: Any, schema_editor: Any) -> None:
    product_model = apps.get_model("catalog", "Product")
    taken: set[str] = set(product_model.objects.exclude(slug="").values_list("slug", flat=True))
    for row in (
        product_model.objects.filter(slug="").order_by("id").only("id", "name_uz", "billz_name", "billz_id")
    ):
        base = slugs.derive_slug(
            row.name_uz, row.billz_name, prefix="mahsulot", fallback=row.billz_id, max_length=SLUG_MAX_LENGTH
        )
        attempt = 1
        while (value := slugs.candidate(base, attempt, max_length=SLUG_MAX_LENGTH)) in taken:
            attempt += 1
        taken.add(value)
        product_model.objects.filter(pk=row.pk).update(slug=value)


class Migration(migrations.Migration):
    dependencies = [
        ("catalog", "0004_banner_link_url"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AddField(
            model_name="category",
            name="seo_description_en",
            field=models.CharField(blank=True, db_default="", default="", max_length=300),
        ),
        migrations.AddField(
            model_name="category",
            name="seo_description_ru",
            field=models.CharField(blank=True, db_default="", default="", max_length=300),
        ),
        migrations.AddField(
            model_name="category",
            name="seo_description_uz",
            field=models.CharField(blank=True, db_default="", default="", max_length=300),
        ),
        migrations.AddField(
            model_name="category",
            name="seo_title_en",
            field=models.CharField(blank=True, db_default="", default="", max_length=120),
        ),
        migrations.AddField(
            model_name="category",
            name="seo_title_ru",
            field=models.CharField(blank=True, db_default="", default="", max_length=120),
        ),
        migrations.AddField(
            model_name="category",
            name="seo_title_uz",
            field=models.CharField(blank=True, db_default="", default="", max_length=120),
        ),
        migrations.AddField(
            model_name="product",
            name="seo_description_en",
            field=models.CharField(blank=True, db_default="", default="", max_length=300),
        ),
        migrations.AddField(
            model_name="product",
            name="seo_description_ru",
            field=models.CharField(blank=True, db_default="", default="", max_length=300),
        ),
        migrations.AddField(
            model_name="product",
            name="seo_description_uz",
            field=models.CharField(blank=True, db_default="", default="", max_length=300),
        ),
        migrations.AddField(
            model_name="product",
            name="seo_title_en",
            field=models.CharField(blank=True, db_default="", default="", max_length=120),
        ),
        migrations.AddField(
            model_name="product",
            name="seo_title_ru",
            field=models.CharField(blank=True, db_default="", default="", max_length=120),
        ),
        migrations.AddField(
            model_name="product",
            name="seo_title_uz",
            field=models.CharField(blank=True, db_default="", default="", max_length=120),
        ),
        migrations.AddField(
            model_name="product",
            name="slug",
            field=models.CharField(blank=True, db_default="", default="", max_length=140),
        ),
        migrations.RunPython(backfill_product_slugs, migrations.RunPython.noop, elidable=True),
        migrations.AddConstraint(
            model_name="product",
            constraint=models.UniqueConstraint(
                condition=models.Q(("slug", ""), _negated=True), fields=("slug",), name="product_slug_unique"
            ),
        ),
        migrations.AddConstraint(
            model_name="product",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    ("slug", ""),
                    models.Q(
                        ("slug__regex", "^[a-z0-9]+(?:-[a-z0-9]+)*$"),
                        models.Q(("slug__regex", "^[0-9]+$"), _negated=True),
                    ),
                    _connector="OR",
                ),
                name="product_slug_format",
            ),
        ),
    ]
