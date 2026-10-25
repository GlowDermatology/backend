"""Read-only admin registrations, the integrations seeder and migration reversibility."""

import importlib
from datetime import datetime, timedelta
from typing import Any

import pytest
from django.apps import apps as django_apps
from django.contrib import admin
from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.db import connection, models
from django.test import Client, RequestFactory
from django.urls import reverse

from glowderma.core import enums, seeding
from glowderma.core.admin import ReadOnlyAdmin
from glowderma.core.seeding import SeedContext
from glowderma.integrations import seeds
from glowderma.integrations.models import IntegrationState, Job, WebhookInbox
from glowderma.integrations.tests.factories import JobFactory, WebhookInboxFactory, staff_actor

ANCHOR = datetime.fromisoformat(seeding.DEFAULT_ANCHOR)
CONTEXT = SeedContext(anchor=ANCHOR, keep_open=True)


@pytest.fixture
def super_client(db: None) -> Client:
    client = Client()
    client.force_login(staff_actor(is_staff=True, is_superuser=True))
    return client


@pytest.mark.django_db
@pytest.mark.parametrize("model", [WebhookInbox, Job, IntegrationState])
def test_models_registered_read_only(model: type[models.Model]) -> None:
    model_admin = admin.site._registry[model]
    assert isinstance(model_admin, ReadOnlyAdmin)
    request = RequestFactory().get("/django-admin/")
    request.user = staff_actor(is_staff=True, is_superuser=True)
    assert model_admin.has_view_permission(request)
    assert not model_admin.has_add_permission(request)
    assert not model_admin.has_change_permission(request)
    assert not model_admin.has_delete_permission(request)
    assert model_admin.list_display != ("__str__",)


def test_admin_pages_render(super_client: Client) -> None:
    row = WebhookInboxFactory.create()
    job = JobFactory.create()
    inbox_admin: Any = admin.site._registry[WebhookInbox]
    assert inbox_admin.result_status(row) == "200"
    assert inbox_admin.result_status(WebhookInboxFactory.create(event_key="k2", result={})) == "-"
    for model in (WebhookInbox, Job, IntegrationState):
        name = model._meta.model_name
        assert super_client.get(reverse(f"admin:integrations_{name}_changelist")).status_code == 200
        assert super_client.get(reverse(f"admin:integrations_{name}_changelist") + "?q=1").status_code == 200
        assert super_client.get(reverse(f"admin:integrations_{name}_add")).status_code == 403
    page = super_client.get(reverse("admin:integrations_webhookinbox_change", args=[row.pk]))
    assert page.status_code == 200 and b"accepted" in page.content
    page = super_client.get(reverse("admin:integrations_job_change", args=[job.pk]))
    assert page.status_code == 200
    state = IntegrationState.objects.get(provider="billz")
    assert (
        super_client.get(reverse("admin:integrations_integrationstate_change", args=[state.pk])).status_code
        == 200
    )


@pytest.mark.django_db
def test_seed_integrations_marks_billz_synced_and_is_idempotent() -> None:
    admin_user = get_user_model().objects.create_user("admin@glowderma.uz", "glowderma-demo-2026")
    seeds.seed_integrations(CONTEXT)
    synced_at = ANCHOR - timedelta(minutes=2)
    assert synced_at.isoformat() == "2026-10-24T12:03:00+05:00"
    billz = IntegrationState.objects.get(provider="billz")
    assert billz.last_sync_at == synced_at and billz.last_success_at == synced_at
    assert IntegrationState.objects.exclude(provider="billz").filter(last_sync_at__isnull=True).count() == 5
    job = Job.objects.get()
    assert job.kind == enums.JobKind.BILLZ_IMPORT and job.state == enums.JobState.SUCCEEDED
    assert job.params == {"mode": "delta"}
    assert job.result == {"created": 0, "updated": 248, "unchanged": 0, "new_from_billz": 0}
    assert job.created_at == synced_at and job.finished_at == synced_at
    assert job.updated_at == synced_at and job.started_at == synced_at and job.heartbeat_at == synced_at
    assert job.created_by == admin_user
    assert billz.updated_at == synced_at
    seeds.seed_integrations(CONTEXT)
    assert Job.objects.count() == 1 and IntegrationState.objects.count() == 6
    assert Job.objects.get().updated_at == synced_at


@pytest.mark.django_db
def test_seed_integrations_refuses_to_run_without_the_seeded_admin() -> None:
    with pytest.raises(RuntimeError, match="accounts seeder first"):
        seeds.seed_integrations(CONTEXT)
    assert not Job.objects.exists()  # atomic: the billz state change rolled back with it
    assert IntegrationState.objects.get(provider="billz").last_sync_at is None


def test_seeder_registered_after_accounts(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(seeding, "_registry", {})
    importlib.reload(seeds)
    assert [(name, order) for name, order, _ in seeding.registered_seeders()] == [("integrations", 20)]


def trigger_exists() -> bool:
    with connection.cursor() as cursor:
        cursor.execute("SELECT 1 FROM pg_trigger WHERE tgname = 'integrations_webhookinbox_append_only'")
        return cursor.fetchone() is not None


@pytest.mark.django_db(transaction=True)
def test_integrations_migrations_reverse_to_zero_and_forward_again() -> None:
    assert trigger_exists()
    call_command("migrate", "integrations", "0001", verbosity=0)
    assert not trigger_exists()
    # 0003's reverse deleted the rows; the post_migrate receiver re-inserted them (the table still exists)
    assert IntegrationState.objects.count() == 6
    call_command("migrate", "integrations", "zero", verbosity=0)
    assert "integrations_webhookinbox" not in connection.introspection.table_names()
    assert "integrations_integrationstate" not in connection.introspection.table_names()
    call_command("migrate", "integrations", verbosity=0)
    assert trigger_exists()
    assert IntegrationState.objects.count() == 6
    assert "integrations_job" in connection.introspection.table_names()


@pytest.mark.django_db
def test_0003_reverse_deletes_the_provider_rows_and_forward_restores_them() -> None:
    module = importlib.import_module("glowderma.integrations.migrations.0003_seed_integration_state")
    module.delete_provider_rows(django_apps, connection.schema_editor)
    assert IntegrationState.objects.count() == 0
    module.create_provider_rows(django_apps, connection.schema_editor)
    module.create_provider_rows(django_apps, connection.schema_editor)  # idempotent
    assert sorted(IntegrationState.objects.values_list("provider", flat=True)) == sorted(module.PROVIDERS)


@pytest.mark.django_db(transaction=True)
def test_flush_keeps_the_provider_rows() -> None:
    """A transactional test ends with pytest-django's flush; ``post_migrate`` restores the six rows, so later
    tests (and a ``--reuse-db`` rerun) never see an empty table."""
    IntegrationState.objects.filter(provider="billz").update(consecutive_failures=3)
    call_command("flush", interactive=False, verbosity=0)
    providers = sorted(IntegrationState.objects.values_list("provider", flat=True))
    assert providers == ["billz", "bts", "click", "eskiz", "fcm", "payme"]
    assert IntegrationState.objects.get(provider="billz").consecutive_failures == 0
    call_command("flush", interactive=False, verbosity=0)  # idempotent
    assert IntegrationState.objects.count() == 6
