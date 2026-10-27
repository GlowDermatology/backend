"""notifications.0005 — a campaign can open an https link (gap ADM-10.1, decision GC-3).

Adds ``Campaign.link_url`` (``""`` unless ``link_kind = "url"``; a constant default, so adding it rewrites
nothing), the ``url`` literal of ``link_kind`` and the widened ``campaign_link_fields`` check (a URL campaign
has no product and its URL starts with ``https://``). Reversible: the reverse first turns every URL campaign
into a campaign without a link (``none``; its URL is lost; inbox rows already delivered keep their own copy).
"""

from typing import Any

from django.db import migrations, models


def drop_url_links(apps: Any, schema_editor: Any) -> None:
    campaign = apps.get_model("notifications", "Campaign")
    campaign.objects.filter(link_kind="url").update(link_kind="none", link_url="")


class Migration(migrations.Migration):
    dependencies = [
        ("notifications", "0004_campaign_cancelled"),
    ]

    operations = [
        migrations.RemoveConstraint(model_name="campaign", name="campaign_link_fields"),
        migrations.AddField(
            model_name="campaign",
            name="link_url",
            field=models.CharField(blank=True, default="", max_length=500),
        ),
        migrations.AlterField(
            model_name="campaign",
            name="link_kind",
            field=models.CharField(
                choices=[("none", "None"), ("product", "Product"), ("url", "Url")],
                default="none",
                max_length=8,
            ),
        ),
        migrations.AddConstraint(
            model_name="campaign",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    models.Q(
                        ("link_kind", "none"),
                        ("link_product__isnull", True),
                        ("link_product_label", ""),
                        ("link_url", ""),
                    ),
                    models.Q(
                        ("link_kind", "product"),
                        ("link_url", ""),
                        models.Q(("link_product_label", ""), _negated=True),
                    ),
                    models.Q(
                        ("link_kind", "url"),
                        ("link_product__isnull", True),
                        ("link_product_label", ""),
                        ("link_url__startswith", "https://"),
                    ),
                    _connector="OR",
                ),
                name="campaign_link_fields",
            ),
        ),
        # forward: nothing to convert; reverse (runs first): URL campaigns lose their link
        migrations.RunPython(migrations.RunPython.noop, drop_url_links),
    ]
