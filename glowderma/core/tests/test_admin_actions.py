"""Ops console actions of ``core`` (glowderma.core.admin_actions): requeue an outbox message, requeue every
dead message of a topic, run the consistency report now — permissions, CSRF/POST, confirm step, audit,
repeat safety and rows the action makes no sense for."""

from __future__ import annotations

from datetime import timedelta
from typing import Any
from unittest import mock

import pytest
from django.core.cache import cache
from django.urls import reverse

from glowderma.core import outbox, timeutils
from glowderma.core.models import AuditEntry, OutboxMessage
from glowderma.core.tests.console import (
    ROLES,
    confirm,
    console_client,
    denied,
    messages_of,
    user_of,
)

pytestmark = pytest.mark.django_db

TOPIC = "test.console"


def message(status: str = "dead", **fields: Any) -> OutboxMessage:
    defaults: dict[str, Any] = {
        "topic": TOPIC,
        "payload": {},
        "dedupe_key": f"console-{OutboxMessage.objects.count()}",
        "status": status,
        "attempts": 10 if status == "dead" else 0,
        "last_error": "boom" if status == "dead" else "",
    }
    defaults.update(fields)
    return OutboxMessage.objects.create(**defaults)


def requeue_url(pk: int) -> str:
    return reverse("admin:core_outboxmessage_requeue", args=[pk])


TOPIC_URL = "admin:core_outboxmessage_requeue_dead_topic"
REPORT_URL = "admin:core_auditentry_run_consistency_report"


@pytest.fixture(autouse=True)
def _clean_cache() -> Any:
    cache.delete("ops:consistency_report")
    yield
    cache.delete("ops:consistency_report")


# --- requeue one message ----------------------------------------------------------------------------------


@pytest.mark.parametrize("role", ROLES)
def test_requeue_permission_matrix(role: str) -> None:
    dead = message()
    response = confirm(console_client(role), requeue_url(dead.pk))
    dead.refresh_from_db()
    if role in ("anon", "customer_like"):
        assert denied(response)
        assert dead.status == "dead"
        assert not AuditEntry.objects.filter(action="ops.outbox_requeued").exists()
    else:
        assert response.status_code == 302
        assert dead.status == "pending" and dead.attempts == 0


def test_get_only_renders_the_confirm_step() -> None:
    dead = message()
    client = console_client("staff")
    response = client.get(requeue_url(dead.pk))
    assert response.status_code == 200
    content = response.content.decode()
    assert 'id="dialog"' in content and "Xabarni qayta navbatga qo‘yish" in content
    assert 'name="confirm"' in content and "csrfmiddlewaretoken" in content
    assert "Faqat menejerlar uchun." not in content
    dead.refresh_from_db()
    assert dead.status == "dead"
    assert not AuditEntry.objects.exists()


def test_post_without_the_confirm_flag_changes_nothing() -> None:
    dead = message()
    response = console_client("staff").post(requeue_url(dead.pk), {})
    assert response.status_code == 200  # the confirm step again
    dead.refresh_from_db()
    assert dead.status == "dead"


def test_post_needs_the_csrf_token() -> None:
    dead = message()
    client = console_client("staff", enforce_csrf_checks=True)
    assert confirm(client, requeue_url(dead.pk)).status_code == 403
    dead.refresh_from_db()
    assert dead.status == "dead"


def test_other_methods_are_refused() -> None:
    dead = message()
    assert console_client("staff").put(requeue_url(dead.pk)).status_code == 405


def test_requeue_audits_with_the_staff_actor_and_says_so() -> None:
    dead = message()
    client = console_client("staff")
    response = confirm(client, requeue_url(dead.pk))
    assert response["Location"] == reverse("admin:core_outboxmessage_change", args=[dead.pk])
    assert messages_of(response) == [("success", f"Xabar #{dead.pk} qayta navbatga qo‘yildi.")]
    entry = AuditEntry.objects.get(action="ops.outbox_requeued")
    staff = user_of(client)
    assert (entry.actor_type, entry.actor_id, entry.entity_type, entry.entity_id) == (
        "staff",
        staff.pk,
        "outbox",
        str(dead.pk),
    )
    assert entry.after == {"topic": TOPIC, "status": "pending"}
    domain = AuditEntry.objects.get(action="outbox.requeued")
    assert domain.actor_id == staff.pk and domain.reason == "ops console"


def test_repeat_is_safe() -> None:
    dead = message()
    client = console_client("staff")
    confirm(client, requeue_url(dead.pk))
    OutboxMessage.objects.filter(pk=dead.pk).update(status="dead")  # e.g. it died again …
    confirm(client, requeue_url(dead.pk))
    assert AuditEntry.objects.filter(action="ops.outbox_requeued").count() == 2
    # … but a message that is no longer dead is left alone: a double submit does nothing
    result = outbox.requeue_dead(dead.pk, reason="test")
    assert result.outcome == "not_dead"
    assert AuditEntry.objects.filter(action="outbox.requeued").count() == 2


def test_a_topic_hook_skip_reason_refuses_the_requeue() -> None:
    dead = message()
    hooks = outbox.RequeueHooks(describe=lambda m: "order 7", skip_reason=lambda m: "already recorded")
    outbox.on_requeue(TOPIC, hooks)
    try:
        client = console_client("staff")
        page = client.get(requeue_url(dead.pk)).content.decode()
        assert "Tafsilot: order 7" in page
        response = confirm(client, requeue_url(dead.pk))
    finally:
        outbox._requeue_hooks.pop(TOPIC, None)
    dead.refresh_from_db()
    assert dead.status == "dead"
    assert messages_of(response) == [
        ("warning", f"Xabar #{dead.pk} qayta navbatga qo‘yilmadi: already recorded"),
    ]
    assert not AuditEntry.objects.filter(action__startswith="ops.").exists()


def test_an_overdue_open_message_kicks_the_dispatcher(django_capture_on_commit_callbacks: Any) -> None:
    late = message("pending", available_at=timeutils.now() - timedelta(hours=3))
    with (
        mock.patch("glowderma.core.tasks.dispatch_outbox.delay") as dispatch,
        django_capture_on_commit_callbacks(execute=True),
    ):
        response = confirm(console_client("staff"), requeue_url(late.pk))
    dispatch.assert_called_once_with()
    late.refresh_from_db()
    assert late.status == "pending"
    assert messages_of(response)[0][0] == "success"
    assert AuditEntry.objects.filter(action="ops.outbox_dispatch_kicked", entity_id=str(late.pk)).exists()


@pytest.mark.parametrize(
    ("status", "fields"),
    [
        ("done", {}),
        ("pending", {}),  # due now: the dispatcher delivers it
        ("failed", {"available_at": timeutils.now() - timedelta(hours=3), "deferred_since": timeutils.now()}),
    ],
)
def test_rows_the_requeue_makes_no_sense_for(status: str, fields: dict[str, Any]) -> None:
    row = message(status, **fields)
    client = console_client("staff")
    page = client.get(requeue_url(row.pk)).content.decode()
    assert "gd-ops-refusal" in page and 'name="_ops_confirm"' not in page
    response = confirm(client, requeue_url(row.pk))
    assert messages_of(response)[0][0] == "warning"
    row.refresh_from_db()
    assert row.status == status
    assert not AuditEntry.objects.filter(action__startswith="ops.").exists()
    change = client.get(reverse("admin:core_outboxmessage_change", args=[row.pk])).content.decode()
    assert requeue_url(row.pk) not in change


def test_the_detail_button_and_the_row_button_show_for_dead_rows_only() -> None:
    dead, delivered = message(), message("done")
    client = console_client("staff")
    change = client.get(reverse("admin:core_outboxmessage_change", args=[dead.pk])).content.decode()
    assert requeue_url(dead.pk) in change and "Qayta navbatga qo‘yish" in change
    listing = client.get(reverse("admin:core_outboxmessage_changelist")).content.decode()
    assert requeue_url(dead.pk) in listing
    assert requeue_url(delivered.pk) not in listing


def test_unknown_message_is_404() -> None:
    assert console_client("staff").get(requeue_url(999_999)).status_code == 404


# --- requeue every dead message of a topic (managers) ---------------------------------------------------


@pytest.mark.parametrize("role", ROLES)
def test_topic_requeue_is_for_managers(role: str) -> None:
    dead = message()
    response = confirm(console_client(role), reverse(TOPIC_URL), topic=TOPIC)
    dead.refresh_from_db()
    if role == "manager":
        assert response.status_code == 302
        assert dead.status == "pending"
    else:
        assert denied(response)
        assert dead.status == "dead"


def test_topic_requeue_button_is_hidden_from_plain_staff() -> None:
    message()
    staff_page = console_client("staff").get(reverse("admin:core_outboxmessage_changelist")).content.decode()
    assert reverse(TOPIC_URL) not in staff_page
    manager_page = (
        console_client("manager").get(reverse("admin:core_outboxmessage_changelist")).content.decode()
    )
    assert reverse(TOPIC_URL) in manager_page


def test_topic_requeue_requeues_the_dead_messages_of_that_topic_only() -> None:
    first, second = message(), message()
    other = message(topic="other.topic")
    client = console_client("manager")
    page = client.get(reverse(TOPIC_URL)).content.decode()
    assert f"{TOPIC} — 2 ta o‘lik" in page and "Faqat menejerlar uchun." in page
    response = confirm(client, reverse(TOPIC_URL), topic=TOPIC)
    assert response["Location"] == reverse("admin:core_outboxmessage_changelist")
    assert messages_of(response) == [
        ("success", f"{TOPIC}: 2 ta xabar qayta navbatga qo‘yildi, 0 ta o‘tkazib yuborildi."),
    ]
    statuses = dict(OutboxMessage.objects.values_list("pk", "status"))
    assert statuses == {first.pk: "pending", second.pk: "pending", other.pk: "dead"}
    entry = AuditEntry.objects.get(action="ops.outbox_topic_requeued")
    assert entry.entity_id == TOPIC and (entry.after or {})["requeued"] == [first.pk, second.pk]
    # repeat: the topic has no dead message left, so it is no longer a choice
    again = confirm(client, reverse(TOPIC_URL), topic=TOPIC)
    assert again.status_code == 200 and "Select a valid choice" not in again.content.decode()
    assert AuditEntry.objects.filter(action="ops.outbox_topic_requeued").count() == 1


def test_topic_requeue_refuses_a_topic_without_dead_messages() -> None:
    message(topic="other.topic")
    response = confirm(console_client("manager"), reverse(TOPIC_URL), topic="nothing.here")
    assert response.status_code == 200  # the form refuses the choice
    assert OutboxMessage.objects.get().status == "dead"


def test_topic_requeue_without_any_dead_message_is_unavailable() -> None:
    message("done")
    client = console_client("manager")
    assert "O‘lik xabarlar yo‘q." in client.get(reverse(TOPIC_URL)).content.decode()
    response = confirm(client, reverse(TOPIC_URL), topic=TOPIC)
    assert messages_of(response) == [("warning", "O‘lik xabarlar yo‘q.")]


# --- consistency report now (managers, once per 10 minutes) ----------------------------------------------


@pytest.mark.parametrize("role", ROLES)
def test_consistency_report_permission_matrix(role: str) -> None:
    with mock.patch("glowderma.core.admin.enqueue_task") as enqueue:
        response = confirm(console_client(role), reverse(REPORT_URL))
    if role == "manager":
        enqueue.assert_called_once_with("reporting.consistency_report")
        assert response.status_code == 302
    else:
        assert denied(response)
        enqueue.assert_not_called()


def test_consistency_report_is_rate_limited_and_audited() -> None:
    client = console_client("manager")
    assert client.get(reverse(REPORT_URL)).status_code == 200
    with mock.patch("glowderma.core.admin.enqueue_task") as enqueue:
        first = confirm(client, reverse(REPORT_URL))
        second = confirm(client, reverse(REPORT_URL))
    enqueue.assert_called_once_with("reporting.consistency_report")
    assert messages_of(first)[0][0] == "success"
    assert messages_of(second)[-1] == (
        "warning",
        "Hisobot yaqinda ishga tushirilgan: 10 daqiqadan keyin qayta urinib ko‘ring.",
    )
    entry = AuditEntry.objects.get(action="ops.consistency_report_requested")
    assert entry.actor_id == user_of(client).pk and entry.entity_type == "report"


def test_consistency_report_broker_failure_is_reported_and_releases_the_cooldown() -> None:
    client = console_client("manager")
    with mock.patch("glowderma.core.admin.enqueue_task", side_effect=OSError("broker down")):
        response = confirm(client, reverse(REPORT_URL))
    assert messages_of(response)[0][0] == "error"
    assert not AuditEntry.objects.filter(action="ops.consistency_report_requested").exists()
    assert cache.get("ops:consistency_report") is None


def test_consistency_report_enqueues_the_registered_task() -> None:
    with mock.patch("glowderma.reporting.tasks.consistency_report.delay") as delay:
        confirm(console_client("manager"), reverse(REPORT_URL))
    delay.assert_called_once_with()


def test_enqueue_task_finds_a_task_whose_module_the_web_process_never_imported() -> None:
    from celery import current_app  # type: ignore[import-untyped]

    from glowderma.core.admin_actions import enqueue_task

    name = "reporting.consistency_report"
    task = current_app.tasks.pop(name)
    try:
        with (
            mock.patch.object(
                current_app.loader,
                "import_default_modules",
                side_effect=lambda: current_app.tasks.register(task),
            ) as discover,
            mock.patch.object(task, "delay") as delay,
        ):
            enqueue_task(name)
        discover.assert_called_once_with()
        delay.assert_called_once_with()
    finally:
        current_app.tasks[name] = task
