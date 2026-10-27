"""catalog.0004 — banners can link to an https URL (gap X-BANNER-URL, decision GC-3).

Adds ``Banner.link_url`` (``""`` unless ``link_kind = "url"``; a constant default, so adding it rewrites
nothing), the ``url`` literal of ``link_kind`` and the widened ``banner_link_kind_targets`` check (a URL
banner has no product or category and its URL starts with ``https://``). Reversible: the reverse first turns
every URL banner into a banner without a link (``none``; its URL is lost), so the narrower check holds again.
"""

from typing import Any

from django.db import migrations, models


def drop_url_links(apps: Any, schema_editor: Any) -> None:
    banner = apps.get_model("catalog", "Banner")
    banner.objects.filter(link_kind="url").update(link_kind="none", link_url="")


class Migration(migrations.Migration):
    dependencies = [
        ("catalog", "0003_product_search_trgm_concurrently"),
    ]

    operations = [
        migrations.RemoveConstraint(model_name="banner", name="banner_link_kind_targets"),
        migrations.AddField(
            model_name="banner",
            name="link_url",
            field=models.CharField(blank=True, default="", max_length=500),
        ),
        migrations.AlterField(
            model_name="banner",
            name="link_kind",
            field=models.CharField(
                choices=[("none", "None"), ("product", "Product"), ("category", "Category"), ("url", "Url")],
                default="none",
                max_length=8,
            ),
        ),
        migrations.AddConstraint(
            model_name="banner",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    models.Q(
                        ("link_category__isnull", True),
                        ("link_kind", "none"),
                        ("link_product__isnull", True),
                        ("link_url", ""),
                    ),
                    models.Q(("link_category__isnull", True), ("link_kind", "product"), ("link_url", "")),
                    models.Q(("link_kind", "category"), ("link_product__isnull", True), ("link_url", "")),
                    models.Q(
                        ("link_category__isnull", True),
                        ("link_kind", "url"),
                        ("link_product__isnull", True),
                        ("link_url__startswith", "https://"),
                    ),
                    _connector="OR",
                ),
                name="banner_link_kind_targets",
            ),
        ),
        # forward: nothing to convert; reverse (runs first): URL banners lose their link
        migrations.RunPython(migrations.RunPython.noop, drop_url_links),
    ]
