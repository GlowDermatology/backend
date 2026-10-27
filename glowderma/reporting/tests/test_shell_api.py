"""Contract §2.13 ``GET audit-log/`` (filters, ordering, export) and §2.10 ``GET nav-counts/``."""

from datetime import datetime
from typing import Any

import pytest
from rest_framework.test import APIClient

from glowderma.accounts.models import StaffUser
from glowderma.accounts.tests.factories import StaffUserFactory
from glowderma.appointments.tests.factories import AppointmentFactory
from glowderma.commerce.tests.factories import OrderFactory
from glowderma.core import audit, seeding, timeutils
from glowderma.core.models import AuditEntry
from glowderma.core.timeutils import TASHKENT

pytestmark = pytest.mark.django_db

ANCHOR = datetime.fromisoformat(seeding.DEFAULT_ANCHOR)
URL = "/api/v1/admin/audit-log/"


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(timeutils, "now", lambda: ANCHOR)


def errors(response: Any) -> list[tuple[str | None, str, str]]:
    return [(e["field"], e["code"], e["message"]) for e in response.data["errors"]]


def entry(at: datetime, **kwargs: Any) -> AuditEntry:
    with seeding.pinned_clock(at):
        return audit.record(**kwargs)


class FakeRequest:
    META = {"REMOTE_ADDR": "203.0.113.7", "HTTP_USER_AGENT": "pytest"}
    client_ip = "203.0.113.7"
    request_id = "6f1c2a0e9b8d4c37"


def test_audit_log_shape_filters_and_ordering(staff_client: APIClient, staff: StaffUser) -> None:
    other = StaffUserFactory.create(full_name="")
    adjusted = entry(
        datetime(2026, 10, 24, 12, 3, tzinfo=TASHKENT),
        action="cashback.adjusted",
        entity_type="customer",
        entity_id="5",
        actor=staff,
        request=FakeRequest(),
        after={"direction": "add", "amount": 2950, "reason": "Kassada QR o‘qilmadi", "balance_after": 51550},
    )
    deleted = entry(
        datetime(2026, 10, 23, 9, 0, tzinfo=TASHKENT),
        action="segment.deleted",
        entity_type="segment",
        entity_id="2",
        actor=other,
        before={"name": "Eski guruh"},
    )
    system = entry(
        datetime(2026, 10, 22, 18, 30, tzinfo=TASHKENT),
        action="outbox.dead",
        entity_type="outbox",
        entity_id="9",
        after={"topic": "push.appointment"},
    )
    response = staff_client.get(URL)
    assert response.status_code == 200
    assert [r["id"] for r in response.data["results"]] == [adjusted.pk, deleted.pk, system.pk]
    assert response.data["results"][0] == {
        "id": adjusted.pk,
        "at": "2026-10-24T12:03:00+05:00",
        "actor": {"id": staff.pk, "display_name": staff.full_name},
        "action": "cashback.adjusted",
        "object_type": "customer",
        "object_id": "5",
        "data": {
            "direction": "add",
            "amount": 2950,
            "reason": "Kassada QR o‘qilmadi",
            "balance_after": 51550,
        },
        "ip": "203.0.113.7",
        "request_id": "6f1c2a0e9b8d4c37",
    }
    fallback = response.data["results"][1]
    assert fallback["data"] == {"name": "Eski guruh"}  # before-image when there is no after-image
    assert fallback["actor"] == {"id": other.pk, "display_name": other.email}  # email when no full name
    assert fallback["ip"] is None and fallback["request_id"] == ""
    assert response.data["results"][2]["actor"] is None

    def ids(**params: Any) -> list[int]:
        return [r["id"] for r in staff_client.get(URL, params).data["results"]]

    assert ids(actor=staff.pk) == [adjusted.pk]
    assert ids(action="segment.deleted") == [deleted.pk]
    assert ids(object_type="outbox") == [system.pk]
    assert ids(object_id="5") == [adjusted.pk]
    assert ids(date_from="2026-10-23") == [adjusted.pk, deleted.pk]
    assert ids(date_to="2026-10-23") == [deleted.pk, system.pk]
    assert ids(date_from="2026-10-23", date_to="2026-10-23") == [deleted.pk]
    assert ids(ordering="at") == [system.pk, deleted.pk, adjusted.pk]
    assert ids(ordering="-at", actor=other.pk, action="segment.deleted") == [deleted.pk]
    assert ids(actor=999_999) == []


@pytest.mark.parametrize(
    "params,expected",
    [
        ({"actor": "x"}, [("actor", "invalid", "Noto‘g‘ri qiymat.")]),
        ({"actor": "0"}, [("actor", "min_value", "Qiymat 1 dan kam bo‘lmasin.")]),
        ({"date_from": "24.10.2026"}, [("date_from", "invalid", "Noto‘g‘ri qiymat.")]),
        ({"ordering": "newest"}, [("ordering", "invalid_choice", "Ruxsat etilmagan qiymat.")]),
        ({"page_size": "7"}, [("page_size", "invalid_choice", "Ruxsat etilmagan qiymat.")]),
    ],
)
def test_audit_log_query_validation(
    staff_client: APIClient, params: dict[str, str], expected: list[Any]
) -> None:
    response = staff_client.get(URL, params)
    assert response.status_code == 400 and errors(response) == expected


def test_audit_log_pagination_and_export(staff_client: APIClient, staff: StaffUser, clock: None) -> None:
    for index in range(25):
        entry(
            datetime(2026, 10, 1, 8, index, tzinfo=TASHKENT),
            action="product.updated",
            entity_type="product",
            entity_id=str(index),
            actor=staff,
            request=FakeRequest(),
        )
    entry(
        datetime(2026, 10, 2, 9, tzinfo=TASHKENT), action="outbox.dead", entity_type="outbox", entity_id="=1"
    )
    page = staff_client.get(URL, {"page_size": 20, "page": 2})
    assert (page.data["count"], page.data["page"], page.data["page_count"], len(page.data["results"])) == (
        26,
        2,
        2,
        6,
    )
    response = staff_client.get(URL, {"export": "csv", "ordering": "at", "date_to": "2026-10-02"})
    assert response.status_code == 200
    assert response["Content-Disposition"] == 'attachment; filename="harakatlar-jurnali-20261024-1205.csv"'
    lines = response.content.decode("utf-8-sig").split("\r\n")
    assert lines[0] == '"ID";"Sana";"Xodim";"Amal";"Obyekt";"Obyekt ID";"IP";"So‘rov ID"'
    assert lines[1].endswith(
        f'"2026-10-01 08:00";"{staff.full_name}";"product.updated";"product";"0";"203.0.113.7";'
        '"6f1c2a0e9b8d4c37"'
    )
    assert lines[26].endswith(
        '"2026-10-02 09:00";"";"outbox.dead";"outbox";"\'=1";"";""'
    )  # system row, formula-safe id
    assert len(lines) == 28  # header + 26 rows + trailing CRLF
    exported = AuditEntry.objects.get(action="export.downloaded")
    assert exported.after == {
        "resource": "audit-log",
        "filters": {"ordering": "at", "date_to": "2026-10-02"},
        "row_count": 26,
        "format": "csv",
    }
    assert staff_client.get(URL, {"export": "xlsx"}).status_code == 200


def test_nav_counts(staff_client: APIClient, clock: None) -> None:
    assert staff_client.get("/api/v1/admin/nav-counts/").data == {
        "orders_processing": 0,
        "appointments_pending": 0,
    }
    OrderFactory.create(paid=True)
    OrderFactory.create(paid=True)
    OrderFactory.create()  # awaiting payment
    OrderFactory.create(delivered=True)
    AppointmentFactory.create(start=datetime(2026, 10, 26, 9, tzinfo=TASHKENT))  # requested, future
    AppointmentFactory.create(start=datetime(2026, 10, 20, 9, tzinfo=TASHKENT))  # requested, past
    AppointmentFactory.create(
        start=datetime(2026, 10, 26, 10, tzinfo=TASHKENT), status="confirmed", confirmed_at=ANCHOR
    )
    response = staff_client.get("/api/v1/admin/nav-counts/")
    assert response.status_code == 200
    assert response.data == {"orders_processing": 2, "appointments_pending": 1}
