"""Contract §2.9 ``GET appointments/range/`` (feat/admin-ux): the week and month boards."""

from datetime import date, datetime, time
from typing import Any

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext
from rest_framework.test import APIClient

from glowderma.appointments import selectors
from glowderma.appointments.tests.factories import AppointmentFactory
from glowderma.core.timeutils import TASHKENT
from glowderma.professionals.models import Professional
from glowderma.professionals.tests.factories import (
    BlockedPeriodFactory,
    ProfessionalFactory,
    WorkingHoursFactory,
    WorkingHoursOverrideFactory,
)

pytestmark = pytest.mark.django_db

URL = "/api/v1/admin/appointments/range/"
MONDAY = date(2026, 10, 26)
SUNDAY = date(2026, 11, 1)
ZERO = {"requested": 0, "confirmed": 0, "attended": 0, "rejected": 0, "cancelled": 0}
CONFIRMED_AT = datetime(2026, 10, 24, 9, tzinfo=TASHKENT)


def at(day: date, hour: int, minute: int = 0) -> datetime:
    return datetime.combine(day, time(hour, minute), tzinfo=TASHKENT)


def errors(response: Any) -> list[tuple[str | None, str, str]]:
    return [(e["field"], e["code"], e["message"]) for e in response.data["errors"]]


@pytest.fixture
def board() -> dict[str, Professional]:
    nodira = ProfessionalFactory.create(name="Nodira", position=0)
    jasur = ProfessionalFactory.create(name="Jasur", position=1)
    gone = ProfessionalFactory.create(name="Ketgan", position=2, is_active=False)
    ProfessionalFactory.create(name="Ishlamaydi", position=3, is_active=False)  # no rows: not listed
    WorkingHoursFactory.create(professional=nodira, weekday=0, start=time(9), end=time(18))
    WorkingHoursFactory.create(
        professional=nodira,
        weekday=1,
        start=time(9),
        end=time(18),
        break_start=time(13),
        break_end=time(14),
    )
    WorkingHoursFactory.create(professional=jasur, weekday=0, start=time(10), end=time(20))
    WorkingHoursOverrideFactory.create(
        professional=jasur, date=date(2026, 10, 27), start=time(12), end=time(16)
    )
    BlockedPeriodFactory.create(professional=nodira, start=at(MONDAY, 16), end=at(MONDAY, 16, 30))
    tuesday, wednesday = date(2026, 10, 27), date(2026, 10, 28)
    AppointmentFactory.create(professional=nodira, start=at(MONDAY, 9))
    AppointmentFactory.create(
        professional=nodira, start=at(MONDAY, 10), status="confirmed", confirmed_at=CONFIRMED_AT
    )
    AppointmentFactory.create(
        professional=jasur,
        start=at(MONDAY, 10),
        status="attended",
        confirmed_at=CONFIRMED_AT,
        attended_at=at(MONDAY, 10, 30),
    )
    AppointmentFactory.create(professional=nodira, start=at(tuesday, 23, 30))  # 18:30 UTC: still Tuesday
    AppointmentFactory.create(professional=jasur, start=at(wednesday, 0, 15))  # 19:15 UTC Tuesday: Wednesday
    AppointmentFactory.create(
        professional=gone,
        start=at(wednesday, 10),
        status="cancelled",
        cancelled_at=CONFIRMED_AT,
        cancelled_by="customer",
    )
    AppointmentFactory.create(professional=nodira, start=at(date(2026, 11, 2), 0))  # after the range
    AppointmentFactory.create(professional=nodira, start=at(date(2026, 10, 25), 23, 55))  # before it
    return {"nodira": nodira, "jasur": jasur, "gone": gone}


def test_week_counts(staff_client: APIClient, board: dict[str, Professional]) -> None:
    response = staff_client.get(URL, {"from": "2026-10-26", "to": "2026-11-01"})
    assert response.status_code == 200, response.data
    body = response.data
    assert set(body) == {"from", "to", "specialists", "days", "schedules", "appointments"}
    assert (body["from"], body["to"]) == ("2026-10-26", "2026-11-01")
    assert body["specialists"] == [
        {"id": board["nodira"].pk, "name": "Nodira", "role": "dermatologist", "is_active": True},
        {"id": board["jasur"].pk, "name": "Jasur", "role": "dermatologist", "is_active": True},
        {"id": board["gone"].pk, "name": "Ketgan", "role": "dermatologist", "is_active": False},
    ]
    assert body["schedules"] is None and body["appointments"] is None
    days = body["days"]
    assert [d["date"] for d in days] == [
        f"2026-{m}-{d:02d}" for m, d in [("10", n) for n in range(26, 32)]
    ] + ["2026-11-01"]
    assert days[0] == {
        "date": "2026-10-26",
        "counts": {**ZERO, "requested": 1, "confirmed": 1, "attended": 1},
        "total": 3,
        "working": True,
        "hours": {"start": "09:00", "end": "20:00"},
    }
    assert days[1] == {
        "date": "2026-10-27",
        "counts": {**ZERO, "requested": 1},
        "total": 1,
        "working": True,
        "hours": {"start": "09:00", "end": "18:00"},  # Nodira's week row ∪ Jasur's override
    }
    assert days[2] == {
        "date": "2026-10-28",
        "counts": {**ZERO, "requested": 1, "cancelled": 1},
        "total": 2,
        "working": False,
        "hours": None,
    }
    assert all(d["total"] == 0 and d["counts"] == ZERO and d["working"] is False for d in days[3:])


def test_one_specialist(staff_client: APIClient, board: dict[str, Professional]) -> None:
    response = staff_client.get(
        URL, {"from": "2026-10-26", "to": "2026-10-28", "specialist_id": str(board["jasur"].pk)}
    )
    assert response.status_code == 200, response.data
    assert [s["id"] for s in response.data["specialists"]] == [board["jasur"].pk]
    monday, tuesday, wednesday = response.data["days"]
    assert monday["counts"] == {**ZERO, "attended": 1} and monday["hours"] == {
        "start": "10:00",
        "end": "20:00",
    }
    assert tuesday["total"] == 0 and tuesday["hours"] == {"start": "12:00", "end": "16:00"}
    assert wednesday["counts"] == {**ZERO, "requested": 1} and wednesday["working"] is False
    # an inactive specialist may be chosen too (its appointments stay actionable)
    gone = staff_client.get(
        URL, {"from": "2026-10-28", "to": "2026-10-28", "specialist_id": str(board["gone"].pk)}
    )
    assert gone.data["specialists"][0]["is_active"] is False
    assert gone.data["days"][0]["counts"] == {**ZERO, "cancelled": 1}


def test_full_detail(staff_client: APIClient, board: dict[str, Professional]) -> None:
    response = staff_client.get(URL, {"from": "2026-10-26", "to": "2026-10-28", "detail": "full"})
    assert response.status_code == 200, response.data
    body = response.data
    nodira, jasur = board["nodira"].pk, board["jasur"].pk
    schedules = {(s["specialist_id"], s["date"]): s for s in body["schedules"]}
    assert len(body["schedules"]) == 3 * 3  # Ketgan is listed: it has an appointment on Wednesday
    clinic = board["nodira"].location
    assert clinic is not None
    assert schedules[(nodira, "2026-10-26")] == {
        "specialist_id": nodira,
        "date": "2026-10-26",
        "working_hours": {"start": "09:00", "end": "18:00"},
        "location": {"id": clinic.pk, "name": clinic.name_uz, "address": clinic.address_uz},
        "blocks": [{"start": "16:00", "end": "16:30", "kind": "busy", "label": "Band qilingan"}],
    }
    assert schedules[(nodira, "2026-10-27")]["blocks"] == [
        {"start": "13:00", "end": "14:00", "kind": "off", "label": None}
    ]
    assert schedules[(jasur, "2026-10-27")]["working_hours"] == {"start": "12:00", "end": "16:00"}
    assert schedules[(board["gone"].pk, "2026-10-26")]["working_hours"] is None
    starts = [a["start"] for a in body["appointments"]]
    assert starts == [
        "2026-10-26T09:00:00+05:00",
        "2026-10-26T10:00:00+05:00",
        "2026-10-26T10:00:00+05:00",
        "2026-10-27T23:30:00+05:00",
        "2026-10-28T00:15:00+05:00",
        "2026-10-28T10:00:00+05:00",
    ]
    item = body["appointments"][0]
    assert set(item) >= {"id", "specialist_id", "customer", "service_id", "status", "requested_via"}
    # the full shape equals the day board's item for the same appointment
    day = staff_client.get("/api/v1/admin/appointments/", {"date": "2026-10-26"}).data
    assert body["appointments"][0] == day["appointments"][0]


@pytest.mark.parametrize(
    "params,expected",
    [
        ({}, [("from", "required", "Majburiy maydon."), ("to", "required", "Majburiy maydon.")]),
        ({"from": "2026-10-26"}, [("to", "required", "Majburiy maydon.")]),
        ({"from": "26.10.2026", "to": "2026-10-27"}, [("from", "invalid", "Noto‘g‘ri qiymat.")]),
        ({"from": "2026-10-27", "to": "2026-10-26"},
         [("to", "empty_range", "Oxirgi sana boshlang‘ich sanadan oldin bo‘lmasin.")]),
        ({"from": "2026-10-01", "to": "2026-11-12"}, [("to", "out_of_range", "Ko‘pi bilan 42 kunlik davr.")]),
        ({"from": "2026-10-26", "to": "2026-11-02", "detail": "full"},
         [("to", "out_of_range", "To‘liq ko‘rinish ko‘pi bilan 7 kunlik davr uchun.")]),
        ({"from": "2026-10-26", "to": "2026-10-26", "detail": "all"},
         [("detail", "invalid_choice", "Ruxsat etilmagan qiymat.")]),
        ({"from": "2026-10-26", "to": "2026-10-26", "specialist_id": "0"},
         [("specialist_id", "min_value", "Qiymat 1 dan kam bo‘lmasin.")]),
        ({"from": "2026-10-26", "to": "2026-10-26", "specialist_id": "999999"},
         [("specialist_id", "does_not_exist", "Mutaxassis topilmadi.")]),
        ({"from": "2026-10-26", "to": "2026-10-26", "specialist_id": "x"},
         [("specialist_id", "invalid", "Noto‘g‘ri qiymat.")]),
    ],
)  # fmt: skip
def test_validation(staff_client: APIClient, params: dict[str, str], expected: list[Any]) -> None:
    response = staff_client.get(URL, params)
    assert response.status_code == 400 and errors(response) == expected


def test_span_limits_are_inclusive(staff_client: APIClient) -> None:
    assert staff_client.get(URL, {"from": "2026-10-01", "to": "2026-11-12"}).status_code == 400
    month = staff_client.get(URL, {"from": "2026-10-01", "to": "2026-11-11"})
    assert month.status_code == 200 and len(month.data["days"]) == 42
    week = staff_client.get(URL, {"from": "2026-10-26", "to": "2026-11-01", "detail": "full"})
    assert week.status_code == 200 and len(week.data["days"]) == 7
    single = staff_client.get(URL, {"from": "2026-10-26", "to": "2026-10-26"})
    assert (
        single.status_code == 200
        and single.data["specialists"] == []
        and single.data["days"][0]["hours"] is None
    )


def test_query_budget_does_not_grow_with_days_or_specialists(
    board: dict[str, Professional], django_assert_max_num_queries: Any
) -> None:
    with django_assert_max_num_queries(8):
        selectors.range_view(date(2026, 10, 1), date(2026, 11, 11))
    with django_assert_max_num_queries(8):
        selectors.range_view(MONDAY, SUNDAY, full=True)

    def count(full: bool) -> int:
        with CaptureQueriesContext(connection) as queries:
            view = selectors.range_view(MONDAY, SUNDAY, full=full)
            if view.appointments is not None:  # the items are serialised without extra queries
                for item in view.appointments:
                    _ = (item.customer.name, item.service.name_uz, item.professional.name)
        return len(queries)

    before = (count(False), count(True))
    for index in range(6):
        extra = ProfessionalFactory.create(position=10 + index)
        WorkingHoursFactory.create(professional=extra, weekday=index)
        AppointmentFactory.create(professional=extra, start=at(MONDAY, 11 + index))
    assert (count(False), count(True)) == before
    assert before[0] <= 6 and before[1] <= 7  # noqa: PLR2004


def test_endpoint_query_budget(staff_client: APIClient, board: dict[str, Professional]) -> None:
    def count(params: dict[str, str]) -> int:
        with CaptureQueriesContext(connection) as queries:
            assert staff_client.get(URL, params).status_code == 200
        return len(queries)

    full = {"from": "2026-10-26", "to": "2026-11-01", "detail": "full"}
    count(full)  # warm the authentication's session cache
    first = count(full)
    assert first <= 8  # the staff user, the range validation and its six queries  # noqa: PLR2004
    for index in range(4):
        AppointmentFactory.create(professional=board["jasur"], start=at(MONDAY, 12 + index))
    assert count(full) == first  # no query per appointment
