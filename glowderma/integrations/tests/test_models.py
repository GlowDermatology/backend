"""WebhookInbox (append-only), Job and IntegrationState (design 01 §2, §8)."""

from datetime import timedelta
from typing import Any

import pytest
from django.db import DatabaseError, IntegrityError, connection, transaction
from django.db.models import ProtectedError

from glowderma.core import enums, timeutils
from glowderma.core.db import purge_window
from glowderma.core.labels import stamp
from glowderma.core.models import AppendOnlyError
from glowderma.integrations.models import IntegrationState, Job, WebhookInbox
from glowderma.integrations.tests.factories import (
    IntegrationStateFactory,
    JobFactory,
    WebhookInboxFactory,
    staff_actor,
)

pytestmark = pytest.mark.django_db


def index_definitions(table: str) -> dict[str, str]:
    with connection.cursor() as cursor:
        cursor.execute("SELECT indexname, indexdef FROM pg_indexes WHERE tablename = %s", [table])
        return dict(cursor.fetchall())


def constraint_names(table: str) -> set[str]:
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT conname FROM pg_constraint WHERE conrelid = %s::regclass AND contype IN ('c', 'u')",
            [table],
        )
        return {row[0] for row in cursor.fetchall()}


def create_fails(model: Any, **kwargs: Any) -> None:
    with pytest.raises(IntegrityError), transaction.atomic():
        model.objects.create(**kwargs)


# --- WebhookInbox ----------------------------------------------------------------------------------------


def test_webhookinbox_str_and_ordering() -> None:
    now = timeutils.now()
    older = WebhookInboxFactory.create(received_at=now - timedelta(minutes=1))
    newer = WebhookInboxFactory.create(received_at=now, event_key="complete:9:1")
    same = WebhookInboxFactory.create(received_at=now, event_key="complete:9:2")
    assert list(WebhookInbox.objects.all()) == [same, newer, older]
    assert str(newer) == "Click complete:9:1 → 200"
    assert str(WebhookInboxFactory.create(event_key="k", result={"body": {}})) == "Click k → ?"


def test_webhookinbox_provider_event_key_unique() -> None:
    WebhookInboxFactory.create(provider="payme", event_key="CreateTransaction:abc")
    with pytest.raises(IntegrityError), transaction.atomic():
        WebhookInboxFactory.create(provider="payme", event_key="CreateTransaction:abc")
    WebhookInboxFactory.create(provider="click", event_key="CreateTransaction:abc")


def test_webhookinbox_orm_mutations_are_refused() -> None:
    row = WebhookInboxFactory.create()
    row.entity_ref = "order:1"
    with pytest.raises(AppendOnlyError):
        row.save()
    with pytest.raises(AppendOnlyError):
        row.delete()
    with pytest.raises(AppendOnlyError):
        WebhookInbox.objects.filter(pk=row.pk).update(entity_ref="order:1")
    with pytest.raises(AppendOnlyError):
        WebhookInbox.objects.filter(pk=row.pk).delete()
    with pytest.raises(AppendOnlyError):
        WebhookInbox.objects.bulk_update([row], ["entity_ref"])
    row.refresh_from_db()
    assert row.entity_ref == "order:10248"


def test_webhookinbox_raw_sql_mutations_are_refused_by_the_trigger() -> None:
    row = WebhookInboxFactory.create()
    with (
        pytest.raises(DatabaseError, match="append-only table integrations_webhookinbox"),
        transaction.atomic(),
        connection.cursor() as cursor,
    ):
        cursor.execute("UPDATE integrations_webhookinbox SET entity_ref = 'x' WHERE id = %s", [row.pk])
    with (
        pytest.raises(DatabaseError, match="append-only table integrations_webhookinbox"),
        transaction.atomic(),
        connection.cursor() as cursor,
    ):
        cursor.execute("DELETE FROM integrations_webhookinbox WHERE id = %s", [row.pk])
    assert WebhookInbox.objects.filter(pk=row.pk).exists()


def test_webhookinbox_purge_window_allows_delete_but_never_update() -> None:
    row = WebhookInboxFactory.create()
    with pytest.raises(DatabaseError, match="append-only"), purge_window(), connection.cursor() as cursor:
        cursor.execute("UPDATE integrations_webhookinbox SET entity_ref = 'x' WHERE id = %s", [row.pk])
    with purge_window(), connection.cursor() as cursor:
        cursor.execute("DELETE FROM integrations_webhookinbox WHERE received_at < %s", [timeutils.now()])
    assert not WebhookInbox.objects.filter(pk=row.pk).exists()


def test_webhookinbox_trigger_and_indexes_in_pg_catalog() -> None:
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT tgname FROM pg_trigger WHERE tgrelid = 'integrations_webhookinbox'::regclass"
            " AND NOT tgisinternal"
        )
        assert [row[0] for row in cursor.fetchall()] == ["integrations_webhookinbox_append_only"]
    indexes = index_definitions("integrations_webhookinbox")
    assert "received_at" in indexes["integr_webhook_provider_time"]
    assert "entity_ref" in indexes["integr_webhook_entity_ref"]
    assert "webhookinbox_provider_event_unique" in constraint_names("integrations_webhookinbox")


# --- Job -------------------------------------------------------------------------------------------------


def test_job_defaults_and_str() -> None:
    job = JobFactory.create()
    assert job.state == enums.JobState.QUEUED and job.attempts == 0 and job.created_by is None
    assert job.progress is None and job.result is None and job.error is None
    assert str(job) == f"Billz importi #{job.pk} (Navbatda)"


def test_job_single_flight_per_kind() -> None:
    JobFactory.create(state=enums.JobState.QUEUED)
    create_fails(Job, kind=enums.JobKind.BILLZ_IMPORT, state=enums.JobState.QUEUED)
    now = timeutils.now()
    create_fails(Job, kind=enums.JobKind.BILLZ_IMPORT, state=enums.JobState.RUNNING, started_at=now)
    JobFactory.create(state=enums.JobState.SUCCEEDED, started_at=now, finished_at=now, result={})


def test_job_state_timestamps_consistent() -> None:
    now = timeutils.now()
    create_fails(Job, kind="billz_import", state="queued", started_at=now)
    create_fails(Job, kind="billz_import", state="running")
    create_fails(Job, kind="billz_import", state="running", started_at=now, finished_at=now)
    create_fails(Job, kind="billz_import", state="succeeded", started_at=now)
    create_fails(Job, kind="billz_import", state="failed", finished_at=now, error={"code": "x"})
    JobFactory.create(state="running", started_at=now)


def test_job_failed_requires_an_error() -> None:
    now = timeutils.now()
    create_fails(Job, kind="billz_import", state="failed", started_at=now, finished_at=now)
    job = JobFactory.create(
        state="failed", started_at=now, finished_at=now, error={"code": "worker_lost", "message": "…"}
    )
    assert job.error == {"code": "worker_lost", "message": "…"}


def test_job_created_by_is_protected_and_ordering_is_newest_first() -> None:
    staff = staff_actor()
    now = timeutils.now()
    older = JobFactory.create(state="succeeded", started_at=now, finished_at=now, created_by=staff)
    Job.objects.filter(pk=older.pk).update(created_at=now - timedelta(hours=1))
    newer = JobFactory.create(created_by=staff)
    assert list(Job.objects.all()) == [newer, older]
    assert list(staff.jobs.all()) == [newer, older]
    with pytest.raises(ProtectedError):
        staff.delete()


def test_job_indexes_and_constraints_in_pg_catalog() -> None:
    indexes = index_definitions("integrations_job")
    assert "kind" in indexes["integr_job_kind_created"] and "created_at" in indexes["integr_job_kind_created"]
    assert "WHERE" in indexes["job_single_flight_per_kind"]
    assert constraint_names("integrations_job") >= {"job_state_timestamps_consistent", "job_failed_has_error"}


# --- IntegrationState ------------------------------------------------------------------------------------


def test_six_provider_rows_come_from_the_data_migration() -> None:
    providers = sorted(IntegrationState.objects.values_list("provider", flat=True))
    assert providers == ["billz", "bts", "click", "eskiz", "fcm", "payme"]
    billz = IntegrationState.objects.get(provider="billz")
    assert billz.last_sync_at is None and billz.consecutive_failures == 0 and billz.data == {}
    assert str(billz) == "Billz: oxirgi sinxronlash hali yo‘q"


def test_integration_state_provider_unique() -> None:
    create_fails(IntegrationState, provider="billz")
    state = IntegrationStateFactory.create(provider="billz")  # get_or_create returns the migration row
    assert IntegrationState.objects.filter(provider="billz").count() == 1
    instant = timeutils.now()
    IntegrationState.objects.filter(pk=state.pk).update(last_sync_at=instant)
    state.refresh_from_db()
    assert str(state) == f"Billz: oxirgi sinxronlash {stamp(instant)}"
