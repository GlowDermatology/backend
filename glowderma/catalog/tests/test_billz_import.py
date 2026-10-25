"""Contract §2.3 (Billz block) and §2.14: import job, single-flight, fake rules end to end, polling, stats.

The fake rules are pinned here as the admin e2e scripts rely on them.
"""

from datetime import datetime
from typing import Any
from unittest.mock import patch

import pytest
from rest_framework.test import APIClient

from glowderma.accounts.models import StaffUser
from glowderma.catalog import services, tasks
from glowderma.catalog.models import Product, ProductStock
from glowderma.catalog.tests.factories import ProductFactory, ProductImageFactory, ProductStockFactory
from glowderma.catalog.tests.helpers import errors_of
from glowderma.core import enums, timeutils
from glowderma.core.models import AuditEntry, OutboxMessage
from glowderma.core.timeutils import TASHKENT
from glowderma.integrations.billz.fake import NEW_ITEMS, FakeBillz
from glowderma.integrations.billz.ports import BillzError, BillzProduct
from glowderma.integrations.models import IntegrationState, Job
from glowderma.integrations.tests.factories import JobFactory

pytestmark = pytest.mark.django_db

IMPORT = "/api/v1/admin/integrations/billz/import/"
JOBS = "/api/v1/admin/jobs/"
STATS = "/api/v1/admin/products/stats/"


def start(client: APIClient, capture: Any, body: dict[str, Any] | None = None) -> Any:
    with capture(execute=True):
        response = client.post(IMPORT, body if body is not None else {})
    return response


def test_import_runs_the_fake_rules_end_to_end(
    staff_client: APIClient, staff: StaffUser, django_capture_on_commit_callbacks: Any
) -> None:
    existing = ProductFactory.create(
        billz_id="4607012301", billz_name="HYALURONIC SERUM 30ML", billz_price=185_000, status="active"
    )
    ProductImageFactory.create(product=existing)
    ProductStockFactory.create(product=existing, quantity=12, synced_at=datetime(2026, 1, 1, tzinfo=TASHKENT))
    response = start(staff_client, django_capture_on_commit_callbacks, {"mode": "delta"})
    assert response.status_code == 202, response.json()
    job = Job.objects.get()
    assert response.json() == {
        "id": job.pk,
        "kind": "billz_import",
        "state": "queued",  # the 202 body is the row as queued; the eager task finished it on commit
        "params": {"mode": "delta"},
        "created_at": timeutils.isoformat(job.created_at),
        "started_at": None,
        "finished_at": None,
        "progress": None,
        "result": None,
        "error": None,
        "created_by": {"id": staff.pk, "display_name": staff.full_name},
    }
    polled = staff_client.get(f"{JOBS}{job.pk}/")
    assert polled.status_code == 200
    body = polled.json()
    assert body["state"] == "succeeded"
    assert body["progress"] == {"done": 4, "total": 4}
    assert body["result"] == {"created": 3, "updated": 0, "unchanged": 1, "new_from_billz": 3}
    assert body["error"] is None and body["started_at"] and body["finished_at"]
    assert body["created_by"] == {"id": staff.pk, "display_name": staff.full_name}
    assert job.celery_task_id == "" or Job.objects.get(pk=job.pk).celery_task_id
    # the three fake NEW products, hidden and flagged
    created = Product.objects.filter(billz_id__in=[item.billz_id for item in NEW_ITEMS]).order_by("billz_id")
    assert [
        (p.billz_id, p.billz_name, p.volume, p.billz_price, p.price, p.status, p.is_new_from_billz)
        for p in created
    ] == [
        ("4607019901", "NEW SERUM 30ML", "30 ml", 199_000, 199_000, "hidden", True),
        ("4607019902", "NEW CREAM 50ML", "50 ml", 249_000, 249_000, "hidden", True),
        ("4607019903", "NEW TONER 200ML", "200 ml", 129_000, 129_000, "hidden", True),
    ]
    assert [ProductStock.objects.get(product=p).quantity for p in created] == [11, 7, 23]
    assert all(p.name_uz == "" and p.category_id is None and not p.images.exists() for p in created)
    assert all(p.last_change_at is None for p in created)
    # the existing product kept everything, only synced_at moved
    existing.refresh_from_db()
    stock = ProductStock.objects.get(product=existing)
    assert (existing.billz_name, existing.billz_price, existing.status, stock.quantity) == (
        "HYALURONIC SERUM 30ML",
        185_000,
        "active",
        12,
    )
    assert stock.synced_at is not None and stock.synced_at > datetime(2026, 1, 1, tzinfo=TASHKENT)
    assert existing.billz_synced_at == stock.synced_at
    state = IntegrationState.objects.get(provider="billz")
    assert state.last_sync_at is not None and state.last_success_at == state.last_sync_at
    assert state.consecutive_failures == 0
    stats = staff_client.get(STATS).json()
    assert stats["billz"] == {"connected": True, "last_sync_at": timeutils.isoformat(state.last_sync_at)}
    assert stats["new_from_billz"] == 3 and stats["hidden"] == 3 and stats["total"] == 4
    started = AuditEntry.objects.get(action="billz.import_started")
    assert started.actor_id == staff.pk and started.entity_id == str(job.pk)
    assert started.after == {"job_id": job.pk, "params": {"mode": "delta"}}
    finished = AuditEntry.objects.get(action="billz.import_finished")
    assert finished.actor_type == "system" and finished.actor_id is None
    assert finished.after == {
        "job_id": job.pk,
        "state": "succeeded",
        "result": {"created": 3, "updated": 0, "unchanged": 1, "new_from_billz": 3},
        "error": None,
    }


def test_second_import_is_idempotent(
    staff_client: APIClient, django_capture_on_commit_callbacks: Any
) -> None:
    ProductStockFactory.create(product=ProductFactory.create(billz_id="4607012301"), quantity=12)
    assert start(staff_client, django_capture_on_commit_callbacks).status_code == 202
    first = {
        (
            p.billz_id,
            p.billz_name,
            p.volume,
            p.billz_price,
            p.price,
            p.status,
            p.is_new_from_billz,
            p.updated_at,
        )
        for p in Product.objects.all()
    }
    new = Product.objects.get(billz_id="4607019901")
    services.update_product(
        new.pk, services.ProductInput(price=205_000, status="hidden"), actor=None
    )  # an admin edit must survive the next import
    response = start(staff_client, django_capture_on_commit_callbacks, {"mode": "full"})
    assert response.status_code == 202
    second_job = Job.objects.get(pk=response.json()["id"])
    assert second_job.state == "succeeded"
    assert second_job.result == {"created": 0, "updated": 0, "unchanged": 4, "new_from_billz": 0}
    assert Product.objects.count() == 4
    edited = Product.objects.get(pk=new.pk)
    assert (
        edited.price == 205_000
        and edited.is_new_from_billz is False
        and edited.last_change_kind == "price_changed"
    )
    untouched = {
        (
            p.billz_id,
            p.billz_name,
            p.volume,
            p.billz_price,
            p.price,
            p.status,
            p.is_new_from_billz,
            p.updated_at,
        )
        for p in Product.objects.exclude(pk=new.pk)
    }
    assert untouched == {row for row in first if row[0] != "4607019901"}
    assert Job.objects.count() == 2


def test_default_mode_delta_and_mode_validation(
    staff_client: APIClient, django_capture_on_commit_callbacks: Any
) -> None:
    response = start(staff_client, django_capture_on_commit_callbacks)
    assert response.status_code == 202 and response.json()["params"] == {"mode": "delta"}
    response = staff_client.post(IMPORT, {"mode": "weekly"})
    assert response.status_code == 400
    assert errors_of(response)["mode"] == ("invalid_choice", "Ruxsat etilmagan qiymat.")
    assert Job.objects.count() == 1


def test_single_flight_returns_the_active_job(staff_client: APIClient) -> None:
    running = JobFactory.create(state="running", started_at=timeutils.now(), heartbeat_at=timeutils.now())
    response = staff_client.post(IMPORT, {"mode": "full"})
    assert response.status_code == 202
    assert response.json()["id"] == running.pk and response.json()["state"] == "running"
    assert Job.objects.count() == 1
    assert not AuditEntry.objects.filter(action="billz.import_started").exists()
    assert not OutboxMessage.objects.exists()
    running.state = "succeeded"
    running.finished_at = timeutils.now()
    running.result = {}
    running.save()
    queued = JobFactory.create(state="queued")  # a finished job frees the slot; a queued one holds it
    assert staff_client.post(IMPORT, {}).json()["id"] == queued.pk
    assert Job.objects.count() == 2


def test_import_failure_marks_the_job_failed(
    staff_client: APIClient, django_capture_on_commit_callbacks: Any
) -> None:
    class DownBillz(FakeBillz):
        def iter_products(self, mode: str) -> Any:
            raise BillzError("timeout")

    with patch("glowderma.catalog.billz.get_port", return_value=DownBillz()):
        response = start(staff_client, django_capture_on_commit_callbacks)
    assert response.status_code == 202
    job = Job.objects.get(pk=response.json()["id"])
    assert job.state == "failed" and job.finished_at is not None
    assert job.error == {
        "code": "provider_unavailable",
        "message": "Tashqi xizmat javob bermadi. Qayta urinib ko‘ring.",
    }
    state = IntegrationState.objects.get(provider="billz")
    assert (
        state.consecutive_failures == 1
        and state.last_error == "Tashqi xizmat javob bermadi. Qayta urinib ko‘ring."
    )
    assert state.last_sync_at is None
    assert staff_client.get(STATS).json()["billz"] == {"connected": False, "last_sync_at": None}
    finished = AuditEntry.objects.get(action="billz.import_finished")
    assert (finished.after or {})["state"] == "failed" and (finished.after or {})["error"][
        "code"
    ] == "provider_unavailable"
    # a later success reconnects
    assert start(staff_client, django_capture_on_commit_callbacks).status_code == 202
    assert staff_client.get(STATS).json()["billz"]["connected"] is True


def test_unexpected_crash_is_recorded_as_server_error(
    staff: StaffUser, django_capture_on_commit_callbacks: Any
) -> None:
    class BrokenBillz(FakeBillz):
        def iter_products(self, mode: str) -> Any:
            raise RuntimeError("boom")

    job = JobFactory.create(created_by=staff)
    with patch("glowderma.catalog.billz.get_port", return_value=BrokenBillz()):
        tasks.run_billz_import(job.pk)
    job.refresh_from_db()
    assert job.state == "failed" and job.error == {"code": "server_error", "message": "Kutilmagan xato."}


def test_task_ignores_jobs_that_are_not_queued(staff: StaffUser) -> None:
    done = JobFactory.create(
        state="succeeded", started_at=timeutils.now(), finished_at=timeutils.now(), result={}
    )
    tasks.run_billz_import(done.pk)
    tasks.run_billz_import(999999)
    done.refresh_from_db()
    assert done.state == "succeeded" and not Product.objects.exists()


def test_outbox_handler_ignores_other_kinds() -> None:
    message = OutboxMessage(
        topic="job.queued", payload={"job_id": 1, "kind": "something_else"}, dedupe_key="x"
    )
    tasks.on_job_queued(message)
    assert not Job.objects.exists()


def test_job_detail_404_and_system_actor(staff_client: APIClient) -> None:
    assert staff_client.get(f"{JOBS}999999/").status_code == 404
    job = JobFactory.create(created_by=None)
    response = staff_client.get(f"{JOBS}{job.pk}/")
    assert response.status_code == 200 and response.json()["created_by"] is None


def test_upsert_updates_only_billz_owned_fields() -> None:
    product = ProductFactory.create(
        billz_id="4607012301",
        billz_name="OLD",
        volume="30 ml",
        billz_price=100_000,
        price=150_000,
        status="active",
    )
    ProductStockFactory.create(product=product, quantity=1)
    before_updated_at = Product.objects.get(pk=product.pk).updated_at
    row = BillzProduct("4607012301", "NEW NAME", "50 ml", 120_000, 9, timeutils.now())
    assert services.upsert_from_billz(row) == "updated"
    product.refresh_from_db()
    assert (product.billz_name, product.volume, product.billz_price, product.price, product.status) == (
        "NEW NAME",
        "50 ml",
        120_000,
        150_000,
        "active",
    )
    assert ProductStock.objects.get(product=product).quantity == 9
    assert product.updated_at == before_updated_at and product.last_change_at is None
    assert services.upsert_from_billz(row) == "unchanged"
    assert (
        services.upsert_from_billz(
            BillzProduct("4607012301", "NEW NAME", "50 ml", 120_000, 10, timeutils.now())
        )
        == "updated"
    )
    no_stock = ProductFactory.create(billz_id="4607012302")
    assert (
        services.upsert_from_billz(
            BillzProduct("4607012302", no_stock.billz_name, "30 ml", 185_000, 4, timeutils.now())
        )
        == "updated"
    )
    assert ProductStock.objects.get(product=no_stock).quantity == 4
    created = services.upsert_from_billz(BillzProduct("4607019999", "X", "", None, 0, timeutils.now()))
    assert created == "created" and Product.objects.get(billz_id="4607019999").price == 0
    current = BillzProduct("4607012301", "NEW NAME", "50 ml", 120_000, 10, timeutils.now())
    assert services.import_products([current]) == {
        "created": 0,
        "updated": 0,
        "unchanged": 1,
        "new_from_billz": 0,
    }


def test_stats_billz_connected_depends_on_last_job_and_adapter(staff_client: APIClient) -> None:
    JobFactory.create(
        state="failed",
        started_at=timeutils.now(),
        finished_at=timeutils.now(),
        error={"code": "provider_unavailable", "message": "x"},
        created_at=datetime(2026, 10, 1, tzinfo=TASHKENT),
    )
    assert staff_client.get(STATS).json()["billz"]["connected"] is False
    JobFactory.create(state="succeeded", started_at=timeutils.now(), finished_at=timeutils.now(), result={})
    assert staff_client.get(STATS).json()["billz"]["connected"] is True

    class Unconfigured(FakeBillz):
        def is_configured(self) -> bool:
            return False

    with patch("glowderma.catalog.billz.get_port", return_value=Unconfigured()):
        assert staff_client.get(STATS).json()["billz"]["connected"] is False


def test_enum_literals_in_job_payload() -> None:
    assert set(enums.JobState.values) == {"queued", "running", "succeeded", "failed"}
    assert set(enums.BillzImportMode.values) == {"delta", "full"}
