"""Ops console actions of ``integrations``: start a Billz delta sync and ask BTS for parcel statuses, from the
jobs list and the provider-state list."""

from __future__ import annotations

from typing import Any
from unittest import mock

import pytest
from django.core.cache import cache
from django.urls import reverse

from glowderma.core import enums
from glowderma.core.models import AuditEntry, OutboxMessage
from glowderma.core.tests.console import ROLES, confirm, console_client, denied, messages_of, user_of
from glowderma.integrations.models import Job

pytestmark = pytest.mark.django_db

BILLZ = "admin:integrations_job_billz_sync"
BTS = "admin:integrations_job_bts_poll"
STATE_BILLZ = "admin:integrations_integrationstate_billz_sync"
STATE_BTS = "admin:integrations_integrationstate_bts_poll"


@pytest.fixture(autouse=True)
def _clean_cache() -> Any:
    cache.delete("ops:bts_poll")
    yield
    cache.delete("ops:bts_poll")


@pytest.mark.parametrize("role", ROLES)
def test_billz_sync_permission_matrix(role: str) -> None:
    response = confirm(console_client(role), reverse(BILLZ))
    if role in ("anon", "customer_like"):
        assert denied(response)
        assert not Job.objects.exists()
    else:
        assert response.status_code == 302
        assert Job.objects.filter(kind=enums.JobKind.BILLZ_IMPORT).count() == 1


def test_billz_sync_get_is_the_confirm_step_only() -> None:
    page = console_client("staff").get(reverse(BILLZ))
    assert page.status_code == 200
    assert "Billz sinxronlashni ishga tushirish" in page.content.decode()
    assert not Job.objects.exists()


def test_billz_sync_needs_csrf_and_post() -> None:
    assert confirm(console_client("staff", enforce_csrf_checks=True), reverse(BILLZ)).status_code == 403
    assert console_client("staff").delete(reverse(BILLZ)).status_code == 405
    assert not Job.objects.exists()


def test_billz_sync_starts_a_delta_import_job_like_the_admin_api() -> None:
    client = console_client("staff")
    response = confirm(client, reverse(BILLZ))
    job = Job.objects.get()
    assert job.params == {"mode": "delta"} and job.created_by_id == user_of(client).pk
    assert response["Location"] == reverse("admin:integrations_job_changelist")
    assert messages_of(response) == [("success", f"Billz sinxronlash navbatga qo‘yildi (vazifa #{job.pk}).")]
    entry = AuditEntry.objects.get(action="ops.billz_sync_requested")
    assert (entry.actor_id, entry.entity_type, entry.entity_id) == (user_of(client).pk, "job", str(job.pk))
    assert AuditEntry.objects.filter(action="billz.import_started", entity_id=str(job.pk)).exists()
    assert OutboxMessage.objects.filter(topic="job.queued", dedupe_key=f"job:{job.pk}").exists()


def test_billz_sync_double_click_joins_the_running_import() -> None:
    client = console_client("staff")
    confirm(client, reverse(BILLZ))
    running = Job.objects.get()
    page = client.get(reverse(BILLZ)).content.decode()
    assert f"Billz importi allaqachon navbatda yoki ishlayapti (vazifa #{running.pk})." in page
    second = confirm(client, reverse(STATE_BILLZ))
    assert messages_of(second)[-1][0] == "warning"
    assert Job.objects.count() == 1
    assert AuditEntry.objects.filter(action="ops.billz_sync_requested").count() == 1


def test_billz_sync_is_refused_while_billz_is_fake_in_production() -> None:
    with mock.patch("glowderma.core.integration_modes.fake_in_production", return_value=True):
        client = console_client("staff")
        assert "Billz soxta rejimda" in client.get(reverse(BILLZ)).content.decode()
        response = confirm(client, reverse(BILLZ))
    assert messages_of(response) == [("warning", "Billz soxta rejimda ishlayapti: sinxronlash o‘chirilgan.")]
    assert not Job.objects.exists()


@pytest.mark.parametrize("role", ROLES)
def test_bts_poll_permission_matrix(role: str) -> None:
    with mock.patch("glowderma.integrations.admin.enqueue_task") as enqueue:
        response = confirm(console_client(role), reverse(BTS))
    if role in ("anon", "customer_like"):
        assert denied(response)
        enqueue.assert_not_called()
    else:
        enqueue.assert_called_once_with("commerce.poll_bts")


def test_bts_poll_enqueues_the_existing_task_once_per_cooldown() -> None:
    client = console_client("staff")
    with mock.patch("glowderma.commerce.tasks.poll_bts.delay") as delay:
        first = confirm(client, reverse(BTS))
        second = confirm(client, reverse(STATE_BTS))
    delay.assert_called_once_with()
    assert messages_of(first)[-1] == ("success", "BTS holat so‘rovi navbatga qo‘yildi.")
    assert messages_of(second)[-1][0] == "warning"
    entry = AuditEntry.objects.get(action="ops.bts_poll_requested")
    assert (entry.actor_id, entry.entity_type, entry.entity_id) == (user_of(client).pk, "integration", "bts")


def test_bts_poll_broker_failure_is_an_error_and_frees_the_cooldown() -> None:
    with mock.patch("glowderma.integrations.admin.enqueue_task", side_effect=OSError("down")):
        response = confirm(console_client("staff"), reverse(BTS))
    assert messages_of(response)[-1][0] == "error"
    assert cache.get("ops:bts_poll") is None
    assert not AuditEntry.objects.filter(action="ops.bts_poll_requested").exists()


def test_bts_poll_is_refused_while_bts_is_fake_in_production() -> None:
    with (
        mock.patch("glowderma.core.integration_modes.fake_in_production", return_value=True),
        mock.patch("glowderma.integrations.admin.enqueue_task") as enqueue,
    ):
        response = confirm(console_client("staff"), reverse(BTS))
    enqueue.assert_not_called()
    assert messages_of(response)[-1][0] == "warning"


def test_both_buttons_on_both_lists() -> None:
    client = console_client("staff")
    jobs = client.get(reverse("admin:integrations_job_changelist")).content.decode()
    assert reverse(BILLZ) in jobs and reverse(BTS) in jobs
    states = client.get(reverse("admin:integrations_integrationstate_changelist")).content.decode()
    assert reverse(STATE_BILLZ) in states and reverse(STATE_BTS) in states
    assert "Billz sinxronlashni ishga tushirish" in states and "BTS holatini so‘rash" in states


def test_webhook_inbox_has_no_reprocess_action() -> None:
    from glowderma.integrations.admin import WebhookInboxAdmin

    assert not getattr(WebhookInboxAdmin, "actions_list", ())
    assert not getattr(WebhookInboxAdmin, "actions_detail", ())
