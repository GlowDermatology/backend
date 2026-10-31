"""Sales & catalog dashboard reads of ``catalog``: stock health and the SEO backlog."""

from datetime import datetime, timedelta
from typing import Any

import pytest

from glowderma.catalog import selectors
from glowderma.catalog.tests.factories import (
    CategoryFactory,
    ProductFactory,
    ProductImageFactory,
    ProductStockFactory,
)
from glowderma.core import enums
from glowderma.core.timeutils import TASHKENT

pytestmark = pytest.mark.django_db
NOW = datetime(2026, 10, 31, 12, 0, tzinfo=TASHKENT)
LONG = "Yumshoq va samarali parvarish uchun mo‘ljallangan, teri to‘siqini tiklaydigan kundalik vosita. " * 2
TITLE = "Retinolli tungi serum — Glow Derma"  # 15..60
META = "Teri yangilanishini tezlashtiradigan retinolli tungi serum: ajinlarni kamaytiradi, rangni tekislaydi."


def test_stock_health_counts_sellable_products(django_assert_num_queries: Any) -> None:
    fresh = NOW - timedelta(hours=1)
    ProductStockFactory.create(quantity=5, synced_at=fresh)
    empty = ProductStockFactory.create(quantity=0, synced_at=fresh)
    negative = ProductStockFactory.create(quantity=-2, synced_at=fresh)
    missing = ProductFactory.create()  # no stock row: out of stock, never "stale"
    stale = ProductStockFactory.create(quantity=3, synced_at=NOW - timedelta(days=2))
    never = ProductStockFactory.create(quantity=3, synced_at=None)
    ProductStockFactory.create(quantity=0, product=ProductFactory.create(status=enums.ProductStatus.HIDDEN))
    ProductFactory.create(is_new_from_billz=True, status=enums.ProductStatus.HIDDEN)
    with django_assert_num_queries(3):
        health = selectors.stock_health(NOW - timedelta(hours=24), limit=10)
    assert health.out_of_stock.count == 3
    assert health.out_of_stock.ids == sorted([empty.product_id, negative.product_id, missing.pk])
    assert (health.stale.count, health.stale.ids) == (2, sorted([stale.product_id, never.product_id]))
    assert health.new_from_billz == 1


def _complete(product: Any) -> Any:
    for language in ("uz", "ru", "en"):
        setattr(product, f"name_{language}", f"Retinolli serum {language}")
        setattr(product, f"description_{language}", LONG)
        setattr(product, f"seo_title_{language}", TITLE)
        setattr(product, f"seo_description_{language}", META)
    product.save()
    ProductImageFactory.create(product=product)
    return product


def test_seo_backlog_counts_issue_codes_once_per_row(django_assert_num_queries: Any) -> None:
    clean = _complete(ProductFactory.create())
    bare = ProductFactory.create(name_uz="", category=None)  # name, descriptions, image, category
    no_image = _complete(ProductFactory.create())
    no_image.images.all().delete()
    ProductFactory.create(status=enums.ProductStatus.HIDDEN, name_uz="")  # hidden: not checked
    category = clean.category
    for language in ("ru", "en"):
        setattr(category, f"name_{language}", f"Serumlar {language}")
    category.save()
    CategoryFactory.create()  # empty category: no image, no translations, no active product
    with django_assert_num_queries(2):
        backlog = selectors.seo_backlog(limit=10)
    assert (backlog.products_checked, backlog.products_with_errors, backlog.products_with_warnings) == (
        3,
        2,
        0,
    )
    found = {(issue.kind, issue.code, issue.severity): (issue.count, issue.ids) for issue in backlog.issues}
    assert found[("product", "images_missing", "error")] == (2, [bare.pk, no_image.pk])
    assert found[("product", "name_missing", "error")] == (1, [bare.pk])
    assert found[("product", "description_missing", "error")] == (1, [bare.pk])
    assert found[("product", "description_missing", "warning")] == (1, [bare.pk])  # ru and en: counted once
    assert found[("product", "category_missing", "warning")] == (1, [bare.pk])
    assert not any(severity == "info" for _kind, _code, severity in found)
    assert found[("category", "no_active_products", "warning")][0] >= 1
    assert backlog.issues[0].severity == "error"  # errors first
    assert backlog.categories_checked == 4  # every active category (each factory product brings one)


def test_the_product_list_stock_filter_selects_the_dashboard_rows(monkeypatch: pytest.MonkeyPatch) -> None:
    """The «Savdo va katalog» tiles link to the product list's «Qoldiq» filter: the list shows every row the
    tile counts (an id list stopped at the first hundred, so a tile of 234 listed 100)."""
    from django.urls import reverse

    from glowderma.core import timeutils
    from glowderma.core.tests.console import console_client

    monkeypatch.setattr(timeutils, "now", lambda: NOW)
    fresh = NOW - timedelta(hours=1)
    ProductStockFactory.create(quantity=5, synced_at=fresh)
    empty = ProductStockFactory.create(quantity=0, synced_at=fresh)
    missing = ProductFactory.create()
    stale = ProductStockFactory.create(quantity=3, synced_at=NOW - timedelta(days=2))
    hidden = ProductStockFactory.create(
        quantity=0, product=ProductFactory.create(status=enums.ProductStatus.HIDDEN)
    )
    health = selectors.stock_health(NOW - selectors.STALE_STOCK_AFTER, limit=10)
    client = console_client("staff")
    url = reverse("admin:catalog_product_changelist")

    def listed(query: str) -> list[int]:
        page = client.get(f"{url}?{query}")
        assert page.status_code == 200
        return sorted(row.pk for row in page.context["cl"].result_list)

    assert (
        listed("status__exact=active&stock=out")
        == health.out_of_stock.ids
        == sorted([empty.product_id, missing.pk])
    )
    assert listed("status__exact=active&stock=stale") == health.stale.ids == [stale.product_id]
    assert hidden.product_id in listed("stock=out")
    assert empty.product_id not in listed("stock=in")


def test_the_sales_tiles_link_to_the_stock_filter() -> None:
    from glowderma.reporting.dashboards import sales

    data = sales.build(NOW, 30)
    hrefs = {tile["title"]: tile["href"] for tile in data["catalog_tiles"]}
    assert hrefs["Tugagan mahsulotlar"] == "/django-admin/catalog/product/?status__exact=active&stock=out"
    assert hrefs["Eskirgan qoldiq"] == "/django-admin/catalog/product/?status__exact=active&stock=stale"


def test_the_seo_filter_lists_every_row_of_a_backlog_issue() -> None:
    """A backlog row's number and the list it links to agree, past the dashboard's id-link limit."""
    from django.urls import reverse

    from glowderma.core.tests.console import console_client
    from glowderma.reporting.dashboards import sales

    products = [ProductFactory.create() for _ in range(3)]  # no images, no descriptions: several issues each
    capped = selectors.seo_backlog(limit=1)
    issue = next(entry for entry in capped.issues if entry.kind == "product" and entry.count >= 3)
    assert len(issue.ids) == 1  # the dashboard's own list stops at its limit
    ids = selectors.seo_issue_ids("product", issue.code, issue.severity)
    assert len(ids) == issue.count and {product.pk for product in products} <= set(ids)

    page = console_client("staff").get(
        reverse("admin:catalog_product_changelist") + f"?seo={issue.code}.{issue.severity}"
    )
    assert page.status_code == 200
    assert sorted(row.pk for row in page.context["cl"].result_list) == sorted(ids)
    plain = console_client("staff").get(reverse("admin:catalog_product_changelist"))
    assert "SEO muammosi" not in plain.content.decode()  # offered only when applied (no checklist run)

    rows = sales.build(NOW, 30)["seo"]
    assert any(row["href"].endswith(f"?seo={issue.code}.{issue.severity}") for row in rows)
