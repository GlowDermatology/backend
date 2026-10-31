"""The product list's stock filters: the «Savdo va katalog» stock tiles link through them, so the list holds
every row behind the number (no capped ``?id__in=`` list)."""

from datetime import timedelta

import pytest
from django.core.cache import cache
from django.test import Client, override_settings

from glowderma.catalog import selectors
from glowderma.catalog.tests.factories import ProductFactory, ProductStockFactory
from glowderma.core import timeutils
from glowderma.reporting.dashboards import sales

pytestmark = pytest.mark.django_db


def _tile(data: dict, title: str) -> dict:  # type: ignore[type-arg]
    return next(tile for tile in data["catalog_tiles"] if tile["title"] == title)


@override_settings(BILLZ_MODE="live")
def test_stock_tiles_link_to_exactly_their_rows(ops_client: Client) -> None:
    cache.clear()
    now = timeutils.now()
    for _ in range(3):
        ProductStockFactory.create(quantity=5, synced_at=now - timedelta(hours=30))  # stale
    ProductStockFactory.create(quantity=0, synced_at=now - timedelta(hours=1))  # out of stock
    ProductFactory.create()  # no stock row: out of stock
    ProductStockFactory.create(quantity=4, synced_at=now - timedelta(minutes=5))  # fine
    health = selectors.stock_health(
        now - sales.STALE_STOCK_AFTER, limit=1
    )  # a tiny id limit: the link ignores it
    data = sales.build(now, 30)
    for title, finding in (("Eskirgan qoldiq", health.stale), ("Tugagan mahsulotlar", health.out_of_stock)):
        tile = _tile(data, title)
        assert "id__in" not in tile["href"]
        response = ops_client.get(tile["href"])
        assert response.status_code == 200
        assert response.context["cl"].result_count == finding.count, title
    assert health.stale.count == 3 and health.out_of_stock.count == 2


def test_stale_stock_is_no_alarm_while_billz_is_fake() -> None:
    cache.clear()
    now = timeutils.now()
    ProductStockFactory.create(quantity=5, synced_at=now - timedelta(hours=30))
    tile = _tile(sales.build(now, 30), "Eskirgan qoldiq")
    assert tile["state"]["label"] == "Sinov rejimi" and tile["muted"] is True
