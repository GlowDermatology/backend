"""The Billz import job over the live adapter (recorded answers, no network) and the M3 beat tasks.

Full sync = reconciliation: products Billz no longer lists are hidden (never deleted) only when the listing
is complete and plausible; the delta watermark moves only after a run was applied; the beat starts the same
single-flight ``billz_import`` job and does nothing while Billz is fake.
"""

from datetime import timedelta
from typing import Any
from unittest.mock import patch

import httpx
import pytest
from celery.exceptions import Retry, SoftTimeLimitExceeded  # type: ignore[import-untyped]
from django.test import override_settings
from redis.exceptions import ConnectionError as RedisConnectionError
from rest_framework.test import APIClient

from glowderma.catalog import services, tasks
from glowderma.catalog.models import Product, ProductStock
from glowderma.catalog.tests.factories import ProductFactory, ProductImageFactory, ProductStockFactory
from glowderma.core import timeutils
from glowderma.core.models import AuditEntry
from glowderma.integrations import selectors as integration_selectors
from glowderma.integrations import services as job_services
from glowderma.integrations.billz.live import LiveBillz
from glowderma.integrations.billz.ports import BillzUnavailable
from glowderma.integrations.models import IntegrationState, Job
from glowderma.integrations.tests.billz_support import BillzServer, Clock, fixture, live
from glowderma.integrations.tests.factories import JobFactory

pytestmark = pytest.mark.django_db

LIVE = {"BILLZ_MODE": "live"}


def run(adapter: LiveBillz, mode: str = "full") -> Job:
    job = JobFactory.create(params={"mode": mode})
    with patch("glowderma.catalog.billz.get_port", return_value=adapter):
        tasks.run_billz_import(job.pk)
    job.refresh_from_db()
    return job


def billz_state() -> IntegrationState:
    return IntegrationState.objects.get(provider="billz")


@override_settings(**LIVE)
def test_full_sync_maps_the_shop_and_hides_what_billz_no_longer_lists() -> None:
    serum = ProductFactory.create(
        billz_id="4607012301", billz_name="OLD NAME", billz_price=150_000, price=190_000
    )
    ProductImageFactory.create(product=serum)
    ProductStockFactory.create(product=serum, quantity=1)
    gone = ProductFactory.create(billz_id="4607012399", status="active")  # Billz dropped it
    ProductImageFactory.create(product=gone)
    already_hidden = ProductFactory.create(billz_id="4607012398", status="hidden")
    job = run(live(BillzServer()))
    assert job.state == "succeeded", job.error
    assert job.result == {"created": 2, "updated": 1, "unchanged": 0, "new_from_billz": 2, "hidden": 1}
    serum.refresh_from_db()
    # Billz-owned fields for BILLZ_SHOP_ID; Glow Derma's price and status untouched
    assert (serum.billz_name, serum.volume, serum.billz_price, serum.price, serum.status) == (
        "HYALURONIC SERUM 30ML",
        "30 ml",
        185_000,
        190_000,
        "active",
    )
    assert ProductStock.objects.get(product=serum).quantity == 12
    foam = Product.objects.get(billz_id="4607012302")
    assert (foam.status, foam.is_new_from_billz, foam.billz_price, foam.price) == (
        "hidden",
        True,
        99_000,
        99_000,
    )
    assert ProductStock.objects.get(product=foam).quantity == 0
    cream = Product.objects.get(billz_id="4607012303")
    assert (cream.billz_price, ProductStock.objects.get(product=cream).quantity) == (None, 0)
    # hidden, never deleted: texts, images and history stay
    gone.refresh_from_db()
    assert gone.status == "hidden" and gone.images.count() == 1
    assert (gone.last_change_kind, gone.last_change_actor_id) == ("hidden", None)
    entry = AuditEntry.objects.get(action="product.updated", entity_id=str(gone.pk))
    assert (entry.actor_type, entry.before, entry.after) == (
        "system",
        {"status": "active"},
        {"status": "hidden"},
    )
    assert entry.reason == services.BILLZ_MISSING_REASON
    already_hidden.refresh_from_db()
    assert already_hidden.last_change_at is None  # nothing to hide twice
    data = integration_selectors.provider_data("billz")
    assert data["full_count"] == 4 and data["delta_since"]
    assert billz_state().last_sync_at is not None


@override_settings(**LIVE)
def test_a_listing_that_breaks_half_way_hides_nothing_and_moves_no_cursor() -> None:
    gone = ProductFactory.create(billz_id="4607012399", status="active")
    services_data = {"delta_since": "2026-10-25T10:00:00+00:00", "full_count": 4}
    job_services.update_provider_data("billz", services_data)
    server = BillzServer()
    server.overrides["GET /v2/products"] = lambda request: (
        httpx.Response(500, json=fixture("internal_error"))
        if request.url.params["page"] == "2"
        else httpx.Response(200, json=fixture("products_page1"))
    )
    job = run(live(server, Clock()))
    assert job.state == "failed"
    assert job.error == {
        "code": "provider_unavailable",
        "message": "Tashqi xizmat javob bermadi. Qayta urinib ko‘ring.",
    }
    gone.refresh_from_db()
    assert gone.status == "active"
    assert not Product.objects.filter(billz_id="4607012301").exists()  # nothing of the run was applied
    assert integration_selectors.provider_data("billz") == services_data
    assert billz_state().last_sync_at is None and billz_state().consecutive_failures == 1


@override_settings(**LIVE)
def test_an_implausibly_small_full_listing_is_refused() -> None:
    gone = ProductFactory.create(billz_id="4607012399", status="active")
    job_services.update_provider_data("billz", {"full_count": 100})
    job = run(live(BillzServer()))
    assert job.state == "failed" and (job.error or {})["code"] == "billz_listing_incomplete"
    assert (job.error or {})["message"] == "Tashqi xizmat javob bermadi. Qayta urinib ko‘ring."
    gone.refresh_from_db()
    assert gone.status == "active" and not Product.objects.filter(billz_id="4607012302").exists()
    assert integration_selectors.provider_data("billz") == {"full_count": 100}


@override_settings(**LIVE)
def test_delta_job_never_hides_and_advances_the_watermark_after_success() -> None:
    gone = ProductFactory.create(billz_id="4607012399", status="active")
    server = BillzServer()
    job = run(live(server), mode="delta")
    assert job.state == "succeeded" and "hidden" not in (job.result or {})
    gone.refresh_from_db()
    assert gone.status == "active"
    data = integration_selectors.provider_data("billz")
    assert set(data) == {"delta_since"}
    run(live(server), mode="delta")
    assert "last_updated_date" in server.requests[-1].url.params


def test_fake_full_import_hides_nothing() -> None:
    known = ProductFactory.create(billz_id="4607012301", status="active")
    job = JobFactory.create(params={"mode": "full"})
    tasks.run_billz_import(job.pk)
    job.refresh_from_db()
    assert job.result == {"created": 3, "updated": 1, "unchanged": 0, "new_from_billz": 3, "hidden": 0}
    known.refresh_from_db()
    assert known.status == "active"
    assert integration_selectors.provider_data("billz") == {}  # the fake keeps no cursor


def test_hide_service_ignores_listed_and_hidden_products() -> None:
    listed = ProductFactory.create(billz_id="4607012301", status="active")
    hidden = ProductFactory.create(billz_id="4607012302", status="hidden")
    missing = ProductFactory.create(billz_id="4607012303", status="active")
    assert services.hide_missing_from_billz({"4607012301"}) == [missing.pk]
    assert services.hide_missing_from_billz({"4607012301"}) == []
    listed.refresh_from_db()
    hidden.refresh_from_db()
    assert listed.status == "active" and hidden.last_change_at is None
    assert Product.objects.count() == 3


def test_hiding_most_of_the_catalog_is_refused() -> None:
    """A barcode-keying mismatch or a wrong shop would make Billz "lose" most products: nothing is hidden."""
    products = ProductFactory.create_batch(10, status="active")
    with pytest.raises(services.TooManyMissing, match="refusing to hide more than 3"):
        services.hide_missing_from_billz({products[0].billz_id}, min_ratio=0.8)
    assert Product.objects.filter(status="active").count() == 10
    # up to the floor (3) or the share (here 20 % of 20 = 4) is fine
    ProductFactory.create_batch(10, status="active")
    listed = set(Product.objects.values_list("billz_id", flat=True)[4:])
    assert len(services.hide_missing_from_billz(listed, min_ratio=0.8)) == 4


@override_settings(**LIVE)
def test_a_full_job_that_would_hide_too_much_fails_and_moves_no_cursor() -> None:
    ProductFactory.create_batch(5, status="active")  # none of them is listed by Billz
    job = run(live(BillzServer()))
    assert job.state == "failed" and (job.error or {})["code"] == "billz_listing_incomplete"
    assert Product.objects.filter(status="active").count() == 5
    assert "full_count" not in integration_selectors.provider_data("billz")


# --- beat ---------------------------------------------------------------------------------------------------


def test_beat_does_nothing_while_billz_is_fake() -> None:
    assert tasks.billz_sync_delta() is None
    assert tasks.billz_sync_full() is None
    assert not Job.objects.exists() and not AuditEntry.objects.exists()


@override_settings(**LIVE)
def test_beat_skips_an_unconfigured_live_adapter(caplog: pytest.LogCaptureFixture) -> None:
    assert tasks.billz_sync_delta() is None
    assert not Job.objects.exists()
    assert "not configured" in caplog.text


@override_settings(**LIVE)
def test_beat_delta_runs_the_import_job_as_the_system(django_capture_on_commit_callbacks: Any) -> None:
    adapter = live(BillzServer())
    with patch("glowderma.catalog.billz.get_port", return_value=adapter):
        with django_capture_on_commit_callbacks(execute=True):
            job_id = tasks.billz_sync_delta()
    job = Job.objects.get(pk=job_id)
    assert (job.state, job.params, job.created_by_id) == ("succeeded", {"mode": "delta"}, None)
    started = AuditEntry.objects.get(action="billz.import_started")
    assert started.actor_type == "system"


@override_settings(**LIVE)
def test_beat_joins_a_running_import_and_the_full_retries_later() -> None:
    running = JobFactory.create(
        state="running", started_at=timeutils.now(), heartbeat_at=timeutils.now(), params={"mode": "delta"}
    )
    adapter = live(BillzServer())
    with patch("glowderma.catalog.billz.get_port", return_value=adapter):
        assert tasks.billz_sync_delta() == running.pk
        with pytest.raises(Retry):
            tasks.billz_sync_full()
        # the last retry gives up for the day instead of retrying forever
        assert tasks.billz_sync_full.apply(retries=tasks.FULL_MAX_RETRIES).get() is None
        running.params = {"mode": "full"}
        running.save()
        assert tasks.billz_sync_full() == running.pk  # an admin full import is already reconciling
    assert Job.objects.count() == 1


def test_beat_entries_are_scheduled_off_the_hour() -> None:
    from config.celery import app

    app.autodiscover_tasks(force=True)
    delta = app.conf.beat_schedule["billz-sync-delta"]
    full = app.conf.beat_schedule["billz-sync-full"]
    assert (delta["task"], full["task"]) == ("catalog.billz_sync_delta", "catalog.billz_sync_full")
    assert delta["schedule"].minute == {7} and len(delta["schedule"].hour) == 24
    assert (full["schedule"].hour, full["schedule"].minute) == ({4}, {23})
    assert "catalog.billz_sync_delta" in app.tasks and "catalog.billz_sync_full" in app.tasks


# --- admin product create with a live Billz that does not answer --------------------------------------------


@override_settings(**LIVE)
def test_product_create_answers_503_when_billz_does_not_answer(staff_client: APIClient) -> None:
    class Down:
        def get_product(self, billz_id: str) -> None:
            raise BillzUnavailable("GET /v2/products: ConnectError")

        def is_configured(self) -> bool:
            return True

    body = {"billz_id": "4607012301", "price": 185_000, "texts": {"uz": {"name": "Serum"}}}
    with patch("glowderma.catalog.billz.get_port", return_value=Down()):
        response = staff_client.post("/api/v1/admin/products/", body)
    assert response.status_code == 503, response.json()
    assert response.json()["code"] == "provider_unavailable"
    assert not Product.objects.exists()


# --- review fixes -------------------------------------------------------------------------------------------


class BrokenCache:
    """Redis is down: every cache operation raises like Django's RedisCache does."""

    def __getattr__(self, name: str) -> Any:
        def fail(*args: Any, **kwargs: Any) -> Any:
            raise RedisConnectionError("Error 111 connecting to localhost:6380. Connection refused.")

        return fail


@override_settings(**LIVE)
@pytest.mark.parametrize("method", ["post", "patch"])
def test_product_create_and_billz_id_change_answer_503_when_redis_is_down(
    staff_client: APIClient, method: str
) -> None:
    """Review B1 (probe test_redis_down_admin_product_create_answers_500_not_503)."""
    existing = ProductFactory.create(billz_id="4607012399")
    body: dict[str, Any] = {"billz_id": "4607012301"}
    if method == "post":
        body.update(price=185_000, texts={"uz": {"name": "Serum"}})
    staff_client.raise_request_exception = False
    with patch("glowderma.catalog.billz.get_port", return_value=live(BillzServer(), cache=BrokenCache())):
        if method == "post":
            response = staff_client.post("/api/v1/admin/products/", body)
        else:
            response = staff_client.patch(f"/api/v1/admin/products/{existing.pk}/", body)
    assert response.status_code == 503, response.content
    assert response.json()["code"] == "provider_unavailable"
    existing.refresh_from_db()
    assert existing.billz_id == "4607012399" and Product.objects.count() == 1


@override_settings(**LIVE)
def test_a_corrupt_billz_answer_on_product_create_is_503(staff_client: APIClient) -> None:
    """Review B7 (probe test_corrupt_gzip_on_admin_create_is_500)."""
    server = BillzServer()
    server.overrides["GET /v2/products"] = lambda request: httpx.Response(
        200, headers={"Content-Encoding": "gzip"}, content=b"garbage"
    )
    staff_client.raise_request_exception = False
    body = {"billz_id": "4607012301", "price": 185_000, "texts": {"uz": {"name": "Serum"}}}
    with patch("glowderma.catalog.billz.get_port", return_value=live(server)):
        response = staff_client.post("/api/v1/admin/products/", body)
    assert response.status_code == 503 and response.json()["code"] == "provider_unavailable"
    assert not Product.objects.exists()


@override_settings(**LIVE)
def test_a_full_sync_with_overlapping_pages_hides_nothing() -> None:
    """Review A M4 (probe test_full_sync_hides_a_product_skipped_by_overlapping_pages): Billz still sells the
    product the shifted pages skipped, so the run is refused instead of hiding it."""
    first, second = fixture("products_page1"), fixture("products_page2")
    second["products"][1] = first["products"][1]  # page 2 repeats page 1's last product; 4607012303 is lost
    server = BillzServer()
    server.product_pages = [first, second]
    skipped = ProductFactory.create(billz_id="4607012303", status="active")
    ProductImageFactory.create(product=skipped)
    job = run(live(server))
    assert job.state == "failed" and (job.error or {})["code"] == "billz_listing_incomplete"
    skipped.refresh_from_db()
    assert skipped.status == "active"


@override_settings(**LIVE)
def test_a_product_created_while_the_full_listing_was_read_is_not_hidden() -> None:
    """Review B12 (probe test_product_activated_during_the_upserts_is_hidden_by_the_full_sync): staff
    created and activated a product Billz added after the listing was read; it is judged by the next run."""
    created: list[int] = []
    original = services.upsert_from_billz

    def upsert(row: Any) -> Any:
        outcome = original(row)
        if not created:
            created.append(ProductFactory.create(billz_id="4607012377", status="active").pk)
        return outcome

    with patch.object(services, "upsert_from_billz", upsert):
        job = run(live(BillzServer()))
    assert job.state == "succeeded", job.error
    assert Product.objects.get(pk=created[0]).status == "active"
    assert job.result is not None and job.result["hidden"] == 0


def test_hide_service_skips_products_changed_after_the_listing_started() -> None:
    old = ProductFactory.create(billz_id="4607012301", status="active")
    edited = ProductFactory.create(billz_id="4607012302", status="active")
    looked_up = ProductFactory.create(billz_id="4607012303", status="active")
    listed_at = timeutils.now()
    Product.objects.filter(pk__in=[old.pk, edited.pk, looked_up.pk]).update(
        created_at=listed_at - timedelta(days=1), updated_at=listed_at - timedelta(days=1)
    )
    Product.objects.filter(pk=edited.pk).update(updated_at=listed_at + timedelta(seconds=1))
    Product.objects.filter(pk=looked_up.pk).update(billz_synced_at=listed_at + timedelta(seconds=1))
    assert services.hide_missing_from_billz(set(), listed_at=listed_at) == [old.pk]


@override_settings(**LIVE)
def test_the_live_sync_never_blanks_the_volume_without_a_volume_field() -> None:
    """Review A S6 (probe test_live_sync_blanks_the_volume_when_billz_volume_field_is_unset): "empty =
    volume not synced" means the stored volume stays, not that it is erased."""
    serum = ProductFactory.create(billz_id="4607012301", volume="30 ml", billz_name="OLD")
    job = run(live(BillzServer(), volume_field=""), mode="delta")
    assert job.state == "succeeded", job.error
    serum.refresh_from_db()
    assert (serum.volume, serum.billz_name) == ("30 ml", "HYALURONIC SERUM 30ML")
    created = Product.objects.get(billz_id="4607012302")
    assert created.volume == ""  # a new product simply starts without one


@override_settings(**LIVE)
def test_a_refused_integration_key_fails_the_job_with_its_own_code() -> None:
    """Review B nit: a revoked key is not "Billz down"; staff must fix BILLZ_SECRET_KEY."""
    server = BillzServer()
    server.overrides["POST /v1/auth/login"] = lambda request: httpx.Response(
        403, json=fixture("login_forbidden")
    )
    job = run(live(server))
    assert job.state == "failed" and (job.error or {})["code"] == "billz_auth_failed"
    assert (job.error or {})["message"] == "Tashqi xizmat javob bermadi. Qayta urinib ko‘ring."


@override_settings(**LIVE)
def test_an_import_out_of_time_fails_its_job() -> None:
    class Slow:
        def iter_products(self, mode: str) -> Any:
            raise SoftTimeLimitExceeded()

    job = run(Slow())  # type: ignore[arg-type]
    assert job.state == "failed" and (job.error or {})["code"] == "provider_unavailable"
    assert tasks.run_billz_import.soft_time_limit == tasks.IMPORT_SOFT_TIME_LIMIT < 15 * 60
    assert tasks.run_billz_import.time_limit == tasks.IMPORT_TIME_LIMIT < 15 * 60


@override_settings(**LIVE)
def test_the_beat_warns_when_it_joins_an_old_job(caplog: pytest.LogCaptureFixture) -> None:
    """Review B14: an import job older than the reaper's threshold is not joined silently."""
    old = JobFactory.create(params={"mode": "delta"})
    Job.objects.filter(pk=old.pk).update(created_at=timeutils.now() - timedelta(minutes=20))
    with patch("glowderma.catalog.billz.get_port", return_value=live(BillzServer())):
        assert tasks.billz_sync_delta() == old.pk
    assert f"joined import job {old.pk}, queued since" in caplog.text


# --- ProductStock.synced_at never moves backwards (M3 integration, NEW-2 edge) -----------------------------


def _stock(quantity: int, synced_at: Any) -> ProductStock:
    return ProductStockFactory.create(quantity=quantity, synced_at=synced_at)


def test_sync_stock_ignores_a_read_older_than_the_stored_one(caplog: pytest.LogCaptureFixture) -> None:
    newer = timeutils.now()
    stock = _stock(4, newer)
    assert services.sync_stock(stock.product, 9, newer - timedelta(minutes=5)) is False
    stock.refresh_from_db()
    assert (stock.quantity, stock.synced_at) == (4, newer)  # neither the quantity nor the instant moved back
    assert any("billz.stock_read_skipped" in record.getMessage() for record in caplog.records)


def test_sync_stock_writes_a_read_at_the_same_instant_or_later() -> None:
    read_at = timeutils.now()
    stock = _stock(4, read_at)
    assert services.sync_stock(stock.product, 4, read_at) is False  # touched, quantity unchanged
    assert services.sync_stock(stock.product, 7, read_at + timedelta(seconds=1)) is True
    stock.refresh_from_db()
    assert (stock.quantity, stock.synced_at) == (7, read_at + timedelta(seconds=1))


def test_sync_stock_writes_over_a_stock_never_read_from_billz() -> None:
    stock = _stock(4, None)
    read_at = timeutils.now()
    assert services.sync_stock(stock.product, 6, read_at) is True
    stock.refresh_from_db()
    assert (stock.quantity, stock.synced_at) == (6, read_at)


def test_an_import_whose_listing_began_before_a_newer_lookup_keeps_the_lookup() -> None:
    """The import's rows carry the instant its listing began; a product looked up in Billz after that (an
    admin billz_id change) holds a newer stock read, which the import must not overwrite."""
    from glowderma.integrations.billz.ports import BillzProduct

    listing_began = timeutils.now()
    stock = _stock(2, listing_began + timedelta(minutes=3))  # the newer lookup
    product = stock.product
    row = BillzProduct(
        billz_id=product.billz_id,
        billz_name=product.billz_name,
        volume=None,
        price=product.billz_price,
        stock=5,
        synced_at=listing_began,
    )
    assert services.upsert_from_billz(row) == "unchanged"
    stock.refresh_from_db()
    assert (stock.quantity, stock.synced_at) == (2, listing_began + timedelta(minutes=3))
