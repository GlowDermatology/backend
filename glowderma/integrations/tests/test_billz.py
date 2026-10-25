"""The Billz port: fake rules (pinned for the admin e2e scripts), registry modes and the job services."""

from collections.abc import Iterator
from datetime import datetime
from typing import Any

import pytest
from django.db import IntegrityError
from django.test import override_settings

from glowderma.core import enums, timeutils
from glowderma.core.models import AuditEntry, OutboxMessage
from glowderma.core.timeutils import TASHKENT
from glowderma.integrations import selectors, services
from glowderma.integrations.billz.fake import NEW_ITEM_IDS, NEW_ITEMS, FakeBillz, synthetic_product
from glowderma.integrations.billz.ports import BillzProduct
from glowderma.integrations.billz.registry import billz_mode, get_billz
from glowderma.integrations.models import IntegrationState, Job
from glowderma.integrations.tests.factories import JobFactory, staff_actor

KNOWN = BillzProduct(
    "4607012301", "HYALURONIC SERUM 30ML", "30 ml", 185_000, 12, datetime(2026, 1, 1, tzinfo=TASHKENT)
)


class Known:
    def __init__(self, rows: list[BillzProduct]) -> None:
        self.rows = rows

    def get(self, billz_id: str) -> BillzProduct | None:
        return next((row for row in self.rows if row.billz_id == billz_id), None)

    def all(self) -> Iterator[BillzProduct]:
        return iter(self.rows)


# --- fake rules ---------------------------------------------------------------------------------------------


def test_get_product_returns_the_known_product_unchanged() -> None:
    assert FakeBillz(Known([KNOWN])).get_product("4607012301") == KNOWN


@pytest.mark.parametrize("billz_id", ["4607010000", "123450000", "99999999990000"])
def test_ids_ending_in_0000_are_not_found(billz_id: str) -> None:
    assert FakeBillz().get_product(billz_id) is None
    assert FakeBillz(Known([KNOWN])).get_product(billz_id) is None


def test_known_product_wins_over_the_not_found_suffix() -> None:
    known = BillzProduct("4607010000", "KNOWN", "", None, 0, timeutils.now())
    assert FakeBillz(Known([known])).get_product("4607010000") == known


@pytest.mark.parametrize(
    ("billz_id", "price", "stock"),
    [
        ("4607012301", 50_000 + (4607012301 % 400) * 1_000, 4607012301 % 37),
        ("123456", 50_000 + (123456 % 400) * 1_000, 123456 % 37),
        ("4607012399", 449_000, 4607012399 % 37),
    ],
)
def test_synthetic_product_formula(billz_id: str, price: int, stock: int) -> None:
    product = FakeBillz().get_product(billz_id)
    assert product is not None
    assert (product.billz_id, product.billz_name, product.volume, product.price, product.stock) == (
        billz_id,
        f"BILLZ ITEM {billz_id}",
        "50 ml",
        price,
        stock,
    )
    again = synthetic_product(billz_id)
    assert (again.billz_name, again.volume, again.price, again.stock) == (
        product.billz_name,
        product.volume,
        product.price,
        product.stock,
    )
    assert product.synced_at.tzinfo is not None


@pytest.mark.parametrize("mode", ["delta", "full"])
def test_iter_products_yields_three_new_then_every_known(mode: Any) -> None:
    other = BillzProduct("4607012302", "GENTLE FOAM", "150 ml", None, -1, timeutils.now())
    rows = list(FakeBillz(Known([KNOWN, other])).iter_products(mode))
    assert [(r.billz_id, r.billz_name, r.volume, r.price, r.stock) for r in rows[:3]] == [
        ("4607019901", "NEW SERUM 30ML", "30 ml", 199_000, 11),
        ("4607019902", "NEW CREAM 50ML", "50 ml", 249_000, 7),
        ("4607019903", "NEW TONER 200ML", "200 ml", 129_000, 23),
    ]
    assert [(r.billz_id, r.billz_name, r.volume, r.price, r.stock) for r in rows[3:]] == [
        ("4607012301", "HYALURONIC SERUM 30ML", "30 ml", 185_000, 12),
        ("4607012302", "GENTLE FOAM", "150 ml", None, -1),
    ]
    assert len({r.synced_at for r in rows}) == 1
    assert list(FakeBillz().iter_products(mode))[3:] == []
    assert NEW_ITEM_IDS == {item.billz_id for item in NEW_ITEMS}


def test_iter_products_does_not_repeat_the_new_items_once_known() -> None:
    known_new = BillzProduct("4607019901", "NEW SERUM 30ML", "30 ml", 199_000, 11, timeutils.now())
    rows = list(FakeBillz(Known([known_new, KNOWN])).iter_products("delta"))
    assert [r.billz_id for r in rows] == ["4607019901", "4607019902", "4607019903", "4607012301"]


def test_fake_is_configured() -> None:
    assert FakeBillz().is_configured() is True


# --- registry -----------------------------------------------------------------------------------------------


def test_registry_fake_is_the_test_default() -> None:
    assert billz_mode() == "fake"
    port = get_billz()
    assert isinstance(port, FakeBillz) and port.get_product("4607010000") is None
    known = Known([KNOWN])
    assert get_billz(known).get_product("4607012301") == KNOWN


@override_settings(BILLZ_MODE="live")
def test_registry_live_is_m3() -> None:
    with pytest.raises(NotImplementedError, match="M3"):
        get_billz()


@override_settings(BILLZ_MODE="", INTEGRATIONS_MODE="fake")
def test_registry_falls_back_to_integrations_mode() -> None:
    assert billz_mode() == "fake"
    with override_settings(INTEGRATIONS_MODE="sandbox"):
        with pytest.raises(ValueError, match="BILLZ_MODE"):
            get_billz()


# --- job services -------------------------------------------------------------------------------------------


@pytest.mark.django_db
def test_start_job_is_single_flight_and_announces_on_the_outbox(
    django_capture_on_commit_callbacks: Any,
) -> None:
    actor = staff_actor()
    with django_capture_on_commit_callbacks(execute=False) as callbacks:
        job, created = services.start_job("billz_import", {"mode": "delta"}, actor=actor)
    assert created and job.state == "queued" and job.created_by_id == actor.pk
    assert callbacks  # dispatch_outbox.delay is scheduled on commit
    message = OutboxMessage.objects.get()
    assert message.topic == "job.queued" and message.dedupe_key == f"job:{job.pk}"
    assert message.payload == {"job_id": job.pk, "kind": "billz_import"}
    assert AuditEntry.objects.filter(action="billz.import_started", entity_id=str(job.pk)).exists()
    again, created_again = services.start_job("billz_import", {"mode": "full"}, actor=actor)
    assert not created_again and again.pk == job.pk
    assert OutboxMessage.objects.count() == 1 and Job.objects.count() == 1
    running = services.claim_job(job.pk)
    assert running is not None and running.state == "running"
    assert services.start_job("billz_import", {}, actor=None)[0].pk == job.pk
    services.finish_job(running, {"created": 0})
    fresh, created_fresh = services.start_job("billz_import", {}, actor=None)
    assert created_fresh and fresh.pk != job.pk


@pytest.mark.django_db
def test_start_job_retries_when_the_competing_job_finished_between_statements(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = {"count": 0}
    original = Job.objects.create

    def flaky_create(**kwargs: Any) -> Job:
        calls["count"] += 1
        if calls["count"] == 1:
            raise IntegrityError("job_single_flight_per_kind")
        return original(**kwargs)

    monkeypatch.setattr(Job.objects, "create", flaky_create)
    job, created = services.start_job("billz_import", {"mode": "delta"})
    assert created and job.pk and calls["count"] == 2


@pytest.mark.django_db
def test_claim_progress_finish_and_fail() -> None:
    job = JobFactory.create()
    assert services.claim_job(999999) is None
    claimed = services.claim_job(job.pk)
    assert claimed is not None
    assert claimed.state == "running" and claimed.attempts == 1 and claimed.started_at == claimed.heartbeat_at
    assert services.claim_job(job.pk) is None  # already running: a redelivery does nothing
    services.report_progress(claimed, 10, 20)
    assert Job.objects.get(pk=job.pk).progress == {"done": 10, "total": 20}
    services.report_progress(claimed, 10, None)
    assert Job.objects.get(pk=job.pk).progress is None
    services.set_celery_task_id(job.pk, "task-id")
    assert Job.objects.get(pk=job.pk).celery_task_id == "task-id"
    finished = services.finish_job(claimed, {"created": 1})
    assert finished.state == "succeeded" and finished.result == {"created": 1} and finished.finished_at
    with pytest.raises(services.JobNotActive):
        services.finish_job(claimed, {})
    with pytest.raises(services.JobNotActive):
        services.fail_job(claimed, "server_error", "x")
    other = services.claim_job(JobFactory.create().pk)
    assert other is not None
    failed = services.fail_job(other, "provider_unavailable", "down")
    assert failed.state == "failed" and failed.error == {"code": "provider_unavailable", "message": "down"}


@pytest.mark.django_db
def test_provider_state_bookkeeping() -> None:
    synced_at = datetime(2026, 10, 24, 11, 58, tzinfo=TASHKENT)
    services.record_failure("billz", "boom")
    services.record_failure("billz", "boom again")
    state = selectors.provider_state("billz")
    assert state is not None and state.consecutive_failures == 2 and state.last_error == "boom again"
    assert state.last_error_at is not None and state.last_sync_at is None
    state = services.record_sync_success("billz", synced_at=synced_at)
    assert state.last_sync_at == synced_at and state.last_success_at == synced_at
    assert state.consecutive_failures == 0 and state.last_error == ""
    assert services.get_state("billz").pk == state.pk
    assert IntegrationState.objects.filter(provider="billz").count() == 1
    assert selectors.provider_state("nope") is None


@pytest.mark.django_db
def test_billz_status_selector() -> None:
    assert selectors.billz_status(configured=True) == {"connected": True, "last_sync_at": None}
    assert selectors.billz_status(configured=False)["connected"] is False
    services.record_sync_success("billz")
    JobFactory.create(
        state=enums.JobState.FAILED,
        started_at=timeutils.now(),
        finished_at=timeutils.now(),
        error={"code": "x"},
    )
    status = selectors.billz_status(configured=True)
    assert status["connected"] is False and status["last_sync_at"] is not None
    assert selectors.latest_job("billz_import") is not None and selectors.latest_job("other") is None
    assert selectors.get_job(999999) is None
