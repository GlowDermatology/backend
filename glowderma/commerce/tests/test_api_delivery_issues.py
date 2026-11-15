"""Admin API of the delivery exception queue and the dispatch switch (Yandex Delivery plan §3.9):
``GET delivery/issues/``, ``POST orders/{id}/issues/``, ``POST delivery/issues/{id}/resolve/``,
``GET|POST delivery/suspension/``."""

import uuid
from datetime import timedelta
from typing import Any

import pytest
from django.test import override_settings
from rest_framework.test import APIClient

from glowderma.commerce.tests.factories import OrderFactory
from glowderma.core import enums, timeutils
from glowderma.core.models import AuditEntry, OutboxMessage
from glowderma.delivery import services as delivery_services
from glowderma.delivery.models import DeliveryIssue, DeliverySettings
from glowderma.delivery.tests.factories import DeliveryIssueFactory

pytestmark = pytest.mark.django_db
ISSUES = "/api/v1/admin/delivery/issues/"
SUSPENSION = "/api/v1/admin/delivery/suspension/"
ISSUE_KEYS = {
    "id",
    "order_id",
    "kind",
    "code",
    "urgent",
    "details",
    "note",
    "state",
    "opened_at",
    "updated_at",
    "reminded_at",
    "reminders",
    "resolved_at",
    "resolved_by",
    "resolution_note",
}


def _post(client: APIClient, path: str, body: Any = None, key: str | None = None) -> Any:
    return client.post(path, body or {}, format="json", HTTP_IDEMPOTENCY_KEY=key or str(uuid.uuid4()))


def _resolve_url(issue_id: int) -> str:
    return f"{ISSUES}{issue_id}/resolve/"


def _open_issues() -> tuple[DeliveryIssue, DeliveryIssue, DeliveryIssue, DeliveryIssue]:
    now = timeutils.now()
    old = DeliveryIssueFactory.create(opened_at=now - timedelta(hours=3))
    urgent = DeliveryIssueFactory.create(
        kind=enums.DeliveryIssueKind.PARCEL_UNACCOUNTED, code="cancelled_by_taxi", urgent=True, opened_at=now
    )
    young = DeliveryIssueFactory.create(kind=enums.DeliveryIssueKind.DISPATCH_REVIEW, opened_at=now)
    done = DeliveryIssueFactory.create(
        state=enums.DeliveryIssueState.RESOLVED,
        opened_at=now - timedelta(days=1),
        resolved_at=now - timedelta(hours=20),
        resolution_note="Qayta chaqirildi",
    )
    return old, urgent, young, done


# --- the queue ----------------------------------------------------------------------------------------------


def test_the_queue_lists_open_issues_urgent_first_then_oldest(staff_client: APIClient) -> None:
    old, urgent, young, done = _open_issues()
    body = staff_client.get(ISSUES).json()
    assert set(body) >= {"count", "page", "page_size", "page_count", "results"}
    assert [row["id"] for row in body["results"]] == [urgent.pk, old.pk, young.pk, done.pk]
    row = body["results"][0]
    assert set(row) == ISSUE_KEYS
    assert (
        row["order_id"] == urgent.order_id and row["kind"] == "parcel_unaccounted" and row["urgent"] is True
    )
    assert (
        row["state"] == "open"
        and row["resolved_by"] is None
        and row["details"] == {"order_id": urgent.order_id}
    )


@pytest.mark.parametrize(
    ("params", "expected"),
    [
        ({"state": "open"}, ["urgent", "old", "young"]),
        ({"state": "resolved"}, ["done"]),
        ({"urgent": "true"}, ["urgent"]),
        ({"urgent": "false", "state": "open"}, ["old", "young"]),
        ({"kind": "dispatch_review"}, ["young"]),
    ],
)
def test_the_queue_filters(staff_client: APIClient, params: dict[str, str], expected: list[str]) -> None:
    rows = dict(zip(("old", "urgent", "young", "done"), _open_issues(), strict=True))
    body = staff_client.get(ISSUES, params).json()
    assert [row["id"] for row in body["results"]] == [rows[name].pk for name in expected]
    order = staff_client.get(ISSUES, {"order": rows["young"].order_id}).json()
    assert [row["id"] for row in order["results"]] == [rows["young"].pk]


@pytest.mark.parametrize(
    "params", [{"state": "closed"}, {"urgent": "maybe"}, {"kind": "nope"}, {"order": "0"}]
)
def test_the_queue_refuses_bad_filters(staff_client: APIClient, params: dict[str, str]) -> None:
    response = staff_client.get(ISSUES, params)
    assert response.status_code == 400
    (error,) = response.json()["errors"]
    assert error["field"] == next(iter(params))


def test_the_queue_needs_a_staff_token(anon_client: APIClient) -> None:
    assert anon_client.get(ISSUES).status_code == 401
    assert anon_client.get(SUSPENSION).status_code == 401


# --- resolve ------------------------------------------------------------------------------------------------


def test_resolve_closes_the_issue_once_and_audits_it(staff_client: APIClient, staff: Any) -> None:
    issue = DeliveryIssueFactory.create()
    key = str(uuid.uuid4())
    response = _post(staff_client, _resolve_url(issue.pk), {"note": "Kuryer qayta chaqirildi"}, key=key)
    assert response.status_code == 200, response.json()
    body = response.json()
    assert body["state"] == "resolved" and body["resolution_note"] == "Kuryer qayta chaqirildi"
    assert body["resolved_by"] == {"id": staff.pk, "display_name": staff.full_name or staff.email}
    assert body["resolved_at"] is not None
    entry = AuditEntry.objects.get(action=delivery_services.AUDIT_ISSUE_RESOLVED)
    assert entry.entity_id == str(issue.pk) and entry.actor_id == staff.pk
    # the same key replays the answer; a new key is refused: it is resolved already
    assert (
        _post(staff_client, _resolve_url(issue.pk), {"note": "Kuryer qayta chaqirildi"}, key=key).json()
        == body
    )
    again = _post(staff_client, _resolve_url(issue.pk), {"note": "Yana bir bor"})
    assert again.status_code == 409 and again.json()["code"] == "conflict"
    assert AuditEntry.objects.filter(action=delivery_services.AUDIT_ISSUE_RESOLVED).count() == 1


def test_resolve_validation(staff_client: APIClient) -> None:
    issue = DeliveryIssueFactory.create()
    assert _post(staff_client, _resolve_url(999_999), {"note": "Hal qilindi"}).status_code == 404
    short = _post(staff_client, _resolve_url(issue.pk), {"note": " a "})
    assert short.status_code == 400 and short.json()["errors"][0]["field"] == "note"
    missing = _post(staff_client, _resolve_url(issue.pk), {})
    assert missing.status_code == 400 and missing.json()["errors"][0]["code"] == "required"
    long_note = _post(staff_client, _resolve_url(issue.pk), {"note": "x" * 1001})
    assert long_note.status_code == 400 and long_note.json()["errors"][0]["code"] == "max_length"
    no_key = staff_client.post(_resolve_url(issue.pk), {"note": "Hal qilindi"}, format="json")
    assert no_key.status_code == 400 and no_key.json()["code"] == "idempotency_key_required"
    issue.refresh_from_db()
    assert issue.state == "open"


# --- staff incidents ----------------------------------------------------------------------------------------


@override_settings(ALERT_WEBHOOK_URL="https://alerts.example.invalid/hook")
def test_staff_open_an_incident_on_a_delivery_order(staff_client: APIClient, staff: Any) -> None:
    order = OrderFactory.create(paid=True)
    url = f"/api/v1/admin/orders/{order.pk}/issues/"
    created = _post(staff_client, url, {"kind": "incident", "note": "Qadoqda boshqa krem bor"})
    assert created.status_code == 201, created.json()
    body = created.json()
    assert set(body) == ISSUE_KEYS
    assert (body["kind"], body["code"], body["urgent"], body["note"]) == (
        "incident",
        "staff_report",
        False,
        "Qadoqda boshqa krem bor",
    )
    assert not OutboxMessage.objects.filter(topic="alert.send").exists()
    refreshed = _post(staff_client, url, {"kind": "incident", "note": "Mahsulot yetishmaydi", "urgent": True})
    assert refreshed.status_code == 200 and refreshed.json()["id"] == body["id"]
    assert refreshed.json()["urgent"] is True
    assert OutboxMessage.objects.get(topic="alert.send").payload["fields"]["order_id"] == order.pk
    entries = AuditEntry.objects.filter(action=delivery_services.AUDIT_ISSUE_OPENED)
    assert entries.count() == 2 and {entry.actor_id for entry in entries} == {staff.pk}
    # the order card lists it
    detail = staff_client.get(f"/api/v1/admin/orders/{order.pk}/").json()
    assert [issue["id"] for issue in detail["delivery"]["open_issues"]] == [body["id"]]


@pytest.mark.parametrize(
    ("body", "field", "code"),
    [
        ({"kind": "dispatch_failed", "note": "Kuryer kelmadi"}, "kind", "invalid_choice"),
        ({"note": "Kuryer kelmadi"}, "kind", "required"),
        ({"kind": "incident", "note": "ab"}, "note", "min_length"),
        ({"kind": "incident"}, "note", "required"),
    ],
)
def test_only_incidents_with_a_note_may_be_opened(
    staff_client: APIClient, body: dict[str, Any], field: str, code: str
) -> None:
    order = OrderFactory.create(paid=True)
    response = _post(staff_client, f"/api/v1/admin/orders/{order.pk}/issues/", body)
    assert response.status_code == 400
    assert {(error["field"], error["code"]) for error in response.json()["errors"]} >= {(field, code)}
    assert not DeliveryIssue.objects.exists()


def test_incidents_need_a_delivery_order(staff_client: APIClient) -> None:
    body = {"kind": "incident", "note": "Noto‘g‘ri mahsulot"}
    assert _post(staff_client, "/api/v1/admin/orders/999999/issues/", body).status_code == 404
    pickup = OrderFactory.create(paid=True, pickup=True)
    refused = _post(staff_client, f"/api/v1/admin/orders/{pickup.pk}/issues/", body)
    assert refused.status_code == 409 and refused.json()["code"] == "conflict"
    assert not DeliveryIssue.objects.exists()


# --- dispatch suspension ------------------------------------------------------------------------------------


def test_suspension_read_and_switch(staff_client: APIClient, staff: Any) -> None:
    body = staff_client.get(SUSPENSION).json()
    assert body == {
        "suspended": False,
        "until": None,
        "note": "",
        "updated_at": body["updated_at"],
    }
    until = (timeutils.now() + timedelta(hours=3)).replace(microsecond=0)
    response = _post(
        staff_client, SUSPENSION, {"suspended": True, "until": until.isoformat(), "note": "Inventar"}
    )
    assert response.status_code == 200, response.json()
    assert response.json()["suspended"] is True and response.json()["note"] == "Inventar"
    assert response.json()["until"] is not None
    row = DeliverySettings.objects.get(pk=1)
    assert row.dispatch_suspended and row.suspended_until == until and row.updated_by_id == staff.pk
    entry = AuditEntry.objects.get(action=delivery_services.AUDIT_SUSPENSION_UPDATED)
    assert entry.actor_id == staff.pk and isinstance(entry.after, dict) and entry.after["suspended"] is True
    assert staff_client.get(SUSPENSION).json()["suspended"] is True
    resumed = _post(staff_client, SUSPENSION, {"suspended": False, "until": until.isoformat()})
    assert resumed.status_code == 200
    assert resumed.json()["suspended"] is False and resumed.json()["until"] is None
    assert DeliverySettings.objects.get(pk=1).suspended_until is None


@pytest.mark.parametrize(
    ("body", "field", "code"),
    [
        ({"until": None}, "suspended", "required"),
        ({"suspended": True, "until": "2026-01-01T10:00:00"}, "until", "invalid"),
        ({"suspended": True, "until": "2020-01-01T10:00:00+05:00"}, "until", "in_past"),
        ({"suspended": True, "note": "x" * 301}, "note", "max_length"),
    ],
)
def test_suspension_validation(staff_client: APIClient, body: dict[str, Any], field: str, code: str) -> None:
    response = _post(staff_client, SUSPENSION, body)
    assert response.status_code == 400
    assert (response.json()["errors"][0]["field"], response.json()["errors"][0]["code"]) == (field, code)
    assert not DeliverySettings.objects.get(pk=1).dispatch_suspended
