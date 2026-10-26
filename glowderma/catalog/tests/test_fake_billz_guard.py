"""No stock from a fake Billz under the production settings (customer contract §2.3, §6.14; admin §2.3)."""

from collections.abc import Iterator
from dataclasses import replace
from typing import Any
from unittest.mock import patch

import pytest
from django.test import override_settings
from rest_framework.test import APIClient

from glowderma.catalog import billz, services, tasks
from glowderma.catalog.models import Product, ProductStock
from glowderma.catalog.tests.factories import ProductFactory, ProductStockFactory
from glowderma.commerce import selectors as commerce_selectors
from glowderma.core import enums, timeutils
from glowderma.integrations.billz.ports import BillzProduct
from glowderma.integrations.models import IntegrationState, Job
from glowderma.integrations.tests.factories import JobFactory

pytestmark = pytest.mark.django_db

MESSAGE = (
    "Billz sinov rejimida: Billz ma’lumotlarini olib bo‘lmaydi. Billz ulanganidan keyin qayta urinib ko‘ring."
)
PRODUCTS = "/api/v1/admin/products/"
IMPORT = "/api/v1/admin/integrations/billz/import/"


@pytest.fixture
def fake_billz_in_production() -> Iterator[dict[str, Any]]:
    """Production posture for Billz, with spies on every port lookup and on ``sync_stock``."""
    calls: dict[str, Any] = {}
    with (
        override_settings(DEV_FEATURES_ALLOWED=False, BILLZ_MODE="fake"),
        patch.object(billz, "get_port", wraps=billz.get_port) as get_port,
        patch.object(services, "sync_stock", wraps=services.sync_stock) as sync_stock,
    ):
        calls["get_port"], calls["sync_stock"] = get_port, sync_stock
        yield calls
    assert calls["sync_stock"].call_count == 0  # never reached through the fake, whatever the test did


def test_import_is_refused_without_a_job(
    staff_client: APIClient, fake_billz_in_production: dict[str, Any]
) -> None:
    response = staff_client.post(IMPORT, {"mode": "full"})
    assert response.status_code == 409
    assert response.json() == {
        "code": "conflict",
        "message": MESSAGE,
        "errors": [],
        "request_id": response.json()["request_id"],
    }
    assert not Job.objects.exists()
    assert fake_billz_in_production["get_port"].call_count == 0


def test_a_job_queued_before_the_switch_fails_without_a_port_call(
    fake_billz_in_production: dict[str, Any],
) -> None:
    job = JobFactory.create(
        kind=enums.JobKind.BILLZ_IMPORT, state=enums.JobState.QUEUED, params={"mode": "delta"}
    )
    state = IntegrationState.objects.get(provider="billz")
    before = state.last_sync_at
    tasks.run_billz_import(job.pk)
    job.refresh_from_db()
    assert job.state == enums.JobState.FAILED
    assert job.error == {"code": "billz_fake_mode", "message": MESSAGE}
    assert fake_billz_in_production["get_port"].call_count == 0
    state.refresh_from_db()
    assert state.last_sync_at == before


def test_create_and_a_billz_id_change_are_refused_other_patches_work(
    staff_client: APIClient, fake_billz_in_production: dict[str, Any]
) -> None:
    created = staff_client.post(
        PRODUCTS, {"billz_id": "4607012301", "texts": {"uz": {"name": "Serum"}}, "price": 185000}
    )
    assert created.status_code == 409 and created.json()["message"] == MESSAGE
    assert not Product.objects.filter(billz_id="4607012301").exists()
    product = ProductFactory.create(billz_id="4607099901", price=100_000, status="hidden")
    ProductStockFactory.create(product=product, quantity=4)
    changed = staff_client.patch(f"{PRODUCTS}{product.pk}/", {"billz_id": "4607012302"}, format="json")
    assert changed.status_code == 409 and changed.json()["message"] == MESSAGE
    same = staff_client.patch(
        f"{PRODUCTS}{product.pk}/", {"billz_id": "4607099901", "price": 120_000}, format="json"
    )
    assert same.status_code == 200, same.json()
    texts = staff_client.patch(
        f"{PRODUCTS}{product.pk}/", {"texts": {"uz": {"name": "Yangi nom"}}}, format="json"
    )
    assert texts.status_code == 200 and texts.json()["price"] == 120_000
    assert fake_billz_in_production["get_port"].call_count == 0
    assert ProductStock.objects.get(product=product).quantity == 4


def test_the_service_layers_refuse_on_their_own(fake_billz_in_production: dict[str, Any]) -> None:
    product = ProductFactory.create(billz_id="4607099902")
    with pytest.raises(services.BillzNotLive):
        services.require_live_billz()
    with pytest.raises(services.BillzNotLive):
        services.create_product(services.ProductInput(), actor=None)
    row = BillzProduct(
        billz_id="4607099902", billz_name="X", volume="", price=1000, stock=9, synced_at=timeutils.now()
    )
    with pytest.raises(services.BillzNotLive):
        services.upsert_from_billz(row)
    changed = services.ProductInput(billz_product=replace(row, billz_id="4607099903"))
    with pytest.raises(services.BillzNotLive):
        services.update_product(product.pk, changed, actor=None)


def test_sync_stock_refuses_as_the_last_layer() -> None:
    product = ProductFactory.create()
    with (
        override_settings(DEV_FEATURES_ALLOWED=False, BILLZ_MODE="fake"),
        pytest.raises(services.BillzNotLive),
    ):
        services.sync_stock(product, 5, None)
    with override_settings(DEV_FEATURES_ALLOWED=False, BILLZ_MODE="live"):
        assert services.sync_stock(product, 5, None) is True


def test_customer_available_stock_is_zero_while_online_selling_is_off() -> None:
    product = ProductFactory.create()
    ProductStockFactory.create(product=product, quantity=7)
    assert commerce_selectors.customer_available_stock([product.pk]) == {
        product.pk: 7
    }  # local/test: B5 value
    with override_settings(DEV_FEATURES_ALLOWED=False, BILLZ_MODE="fake"):
        assert commerce_selectors.customer_available_stock([product.pk, 999]) == {product.pk: 0, 999: 0}
