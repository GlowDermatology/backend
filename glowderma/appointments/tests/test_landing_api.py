"""The storefront landing page's booking reads (feat/live-landing): ``GET specialists/availability/`` (the
next free slot of every listed specialist, by the rules of ``specialists/{id}/slots/``), ``GET clinics/``
and the public cache of the specialist and service lists."""

from __future__ import annotations

from datetime import date, datetime, time
from typing import Any

import pytest
from django.core.exceptions import ValidationError
from django.db import connection
from django.test import override_settings
from django.test.utils import CaptureQueriesContext
from rest_framework.test import APIClient

from glowderma.appointments import selectors
from glowderma.appointments.tests.factories import AppointmentFactory
from glowderma.core import enums
from glowderma.core.timeutils import TASHKENT
from glowderma.professionals.api.admin.serializers.specialists import ClinicSpecialistWriteSerializer
from glowderma.professionals.models import Professional, Service, professional_slug_base
from glowderma.professionals.tests.factories import (
    BlockedPeriodFactory,
    LocationFactory,
    ProfessionalFactory,
    ServiceFactory,
    WorkingHoursFactory,
    WorkingHoursOverrideFactory,
)

pytestmark = pytest.mark.django_db

AVAILABILITY = "/api/v1/specialists/availability/"
CLINICS = "/api/v1/clinics/"
MONDAY = date(2026, 11, 2)
TUESDAY = date(2026, 11, 3)


def at(day: date, hour: int, minute: int = 0) -> datetime:
    return datetime.combine(day, time(hour, minute), tzinfo=TASHKENT)


@pytest.fixture
def now(monkeypatch: pytest.MonkeyPatch) -> dict[str, datetime]:
    """Monday 2 Nov 2026 08:00 Tashkent; set ``state["now"]`` to move it."""
    state = {"now": at(MONDAY, 8)}
    monkeypatch.setattr("glowderma.core.timeutils.now", lambda: state["now"])
    return state


def specialist(
    *, weekday: int = 0, location: Any = None, start: time = time(9, 0), end: time = time(18, 0)
) -> Service:
    """A published specialist working ``weekday`` (break 13–14) with one 30-minute service."""
    professional = ProfessionalFactory.create(location=location or LocationFactory.create(), slot_step_min=30)
    service = ServiceFactory.create(professional=professional, duration_min=30)
    WorkingHoursFactory.create(
        professional=professional,
        weekday=weekday,
        start=start,
        end=end,
        break_start=time(13, 0) if end > time(14, 0) else None,
        break_end=time(14, 0) if end > time(14, 0) else None,
    )
    return service


def row_of(body: dict[str, Any], professional_id: int) -> dict[str, Any]:
    return next(row for row in body["results"] if row["specialist_id"] == professional_id)


# --- specialists/availability/ ------------------------------------------------------------------------------


def test_availability_shape_next_slot_and_today_count(
    public_client: APIClient, now: dict[str, datetime]
) -> None:
    service = specialist()
    response = public_client.get(AVAILABILITY)
    assert response.status_code == 200, response.content
    assert response["Cache-Control"] == "public, max-age=60"
    body = response.json()
    assert set(body) == {"generated_at", "results"}
    assert datetime.fromisoformat(body["generated_at"]) == now["now"]
    row = row_of(body, service.professional_id)
    assert set(row) == {"specialist_id", "next_slot", "slots_today"}
    assert row["next_slot"] == {
        "start": at(MONDAY, 9).isoformat(),
        "location_id": service.professional.location_id,
        "service_id": service.pk,
    }
    assert row["slots_today"] == 16  # 09:00–12:30 and 14:00–17:30


def test_availability_matches_the_slots_route(public_client: APIClient, now: dict[str, datetime]) -> None:
    """One rule: today's count and the first slot are exactly what ``specialists/{id}/slots/`` offers."""
    service = specialist()
    professional = service.professional
    AppointmentFactory.create(professional=professional, service=service, start=at(MONDAY, 9))
    AppointmentFactory.create(
        professional=professional,
        service=service,
        start=at(MONDAY, 10),
        status=enums.AppointmentStatus.REJECTED,
        rejected_at=at(MONDAY, 7),
    )
    BlockedPeriodFactory.create(professional=professional, start=at(MONDAY, 15), end=at(MONDAY, 16))
    now["now"] = at(MONDAY, 8, 45)  # lead time 60 min: 09:45 is the earliest start
    slots = public_client.get(
        f"/api/v1/specialists/{professional.pk}/slots/",
        {"date": MONDAY.isoformat(), "service_id": service.pk, "location_id": professional.location_id},
    ).json()["slots"]
    row = row_of(public_client.get(AVAILABILITY).json(), professional.pk)
    assert row["slots_today"] == len(slots) == 12
    assert row["next_slot"]["start"] == slots[0]["start_at"] == at(MONDAY, 10).isoformat()


def test_availability_uses_the_shortest_active_service(
    public_client: APIClient, now: dict[str, datetime]
) -> None:
    service = specialist(start=time(9, 0), end=time(10, 0))
    professional = service.professional
    service.duration_min = 60
    service.save()
    ServiceFactory.create(professional=professional, duration_min=15, is_active=False)
    shortest = ServiceFactory.create(professional=professional, duration_min=20)
    row = row_of(public_client.get(AVAILABILITY).json(), professional.pk)
    assert row["next_slot"]["service_id"] == shortest.pk
    assert row["slots_today"] == 2  # 09:00, 09:30 (09:30 + 20 ≤ 10:00); 60 minutes would give one


def test_availability_looks_ahead_and_honours_days(
    public_client: APIClient, now: dict[str, datetime]
) -> None:
    service = specialist(weekday=TUESDAY.weekday())
    row = row_of(public_client.get(AVAILABILITY).json(), service.professional_id)
    assert row["slots_today"] == 0
    assert row["next_slot"]["start"] == at(TUESDAY, 9).isoformat()
    one_day = row_of(public_client.get(AVAILABILITY, {"days": 1}).json(), service.professional_id)
    assert one_day == {"specialist_id": service.professional_id, "next_slot": None, "slots_today": 0}


def test_availability_stops_at_the_booking_horizon(
    public_client: APIClient, now: dict[str, datetime]
) -> None:
    service = specialist(weekday=TUESDAY.weekday())
    with override_settings(BOOKING_HORIZON_DAYS=0):
        row = row_of(public_client.get(AVAILABILITY, {"days": 14}).json(), service.professional_id)
    assert row["next_slot"] is None


def test_availability_after_hours_moves_to_the_next_working_day(
    public_client: APIClient, now: dict[str, datetime]
) -> None:
    service = specialist()
    WorkingHoursFactory.create(professional=service.professional, weekday=TUESDAY.weekday())
    now["now"] = at(MONDAY, 17, 1)  # the lead time leaves nothing today
    row = row_of(public_client.get(AVAILABILITY).json(), service.professional_id)
    assert row["slots_today"] == 0
    assert row["next_slot"]["start"] == at(TUESDAY, 9).isoformat()


def test_availability_skips_a_day_at_an_unassigned_or_closed_clinic(
    public_client: APIClient, now: dict[str, datetime]
) -> None:
    service = specialist()
    professional = service.professional
    WorkingHoursOverrideFactory.create(
        professional=professional, date=MONDAY, location=LocationFactory.create()
    )
    closed = LocationFactory.create(is_active=False)
    professional.additional_locations.add(closed)
    WorkingHoursOverrideFactory.create(professional=professional, date=TUESDAY, location=closed)
    WorkingHoursFactory.create(professional=professional, weekday=2)
    row = row_of(public_client.get(AVAILABILITY).json(), professional.pk)
    assert row["slots_today"] == 0
    assert row["next_slot"]["start"] == at(date(2026, 11, 4), 9).isoformat()


def test_availability_additional_clinic_day(public_client: APIClient, now: dict[str, datetime]) -> None:
    service = specialist()
    other = LocationFactory.create()
    service.professional.additional_locations.add(other)
    WorkingHoursOverrideFactory.create(professional=service.professional, date=MONDAY, location=other)
    row = row_of(public_client.get(AVAILABILITY).json(), service.professional_id)
    assert row["next_slot"]["location_id"] == other.pk
    assert row["next_slot"]["start"] == at(MONDAY, 10).isoformat()  # the override's 10:00–16:00


def test_availability_without_services_or_specialists(
    public_client: APIClient, now: dict[str, datetime]
) -> None:
    assert public_client.get(AVAILABILITY).json()["results"] == []
    professional = ProfessionalFactory.create()
    WorkingHoursFactory.create(professional=professional, weekday=0)
    ProfessionalFactory.create(is_active=False)  # a draft is not listed
    body = public_client.get(AVAILABILITY).json()
    assert body["results"] == [{"specialist_id": professional.pk, "next_slot": None, "slots_today": 0}]


@pytest.mark.parametrize("days", ["0", "15", "x"])
def test_availability_days_is_validated(
    public_client: APIClient, now: dict[str, datetime], days: str
) -> None:
    response = public_client.get(AVAILABILITY, {"days": days})
    assert response.status_code == 400
    assert [error["field"] for error in response.json()["errors"]] == ["days"]


def test_availability_costs_the_same_queries_for_more_specialists(
    public_client: APIClient, now: dict[str, datetime]
) -> None:
    def cost() -> int:
        with CaptureQueriesContext(connection) as captured:
            assert public_client.get(AVAILABILITY, {"days": 14}).status_code == 200
        return len(captured.captured_queries)

    first = specialist()
    AppointmentFactory.create(professional=first.professional, service=first, start=at(MONDAY, 9))
    small = cost()
    for _ in range(4):
        extra = specialist(weekday=TUESDAY.weekday())
        AppointmentFactory.create(professional=extra.professional, service=extra, start=at(TUESDAY, 9))
    assert cost() == small
    assert small <= 8


def test_availability_route_wins_over_the_slug_route(
    public_client: APIClient, now: dict[str, datetime]
) -> None:
    professional = ProfessionalFactory.create(name="Availability")
    assert professional.slug == "mutaxassis-availability"
    assert public_client.get(f"/api/v1/specialists/{professional.slug}/").status_code == 200
    assert "results" in public_client.get(AVAILABILITY).json()


def test_the_availability_slug_is_reserved() -> None:
    assert professional_slug_base("Availability") == "mutaxassis-availability"
    assert professional_slug_base("Availability Clinic") == "availability-clinic"
    serializer = ClinicSpecialistWriteSerializer(data={"slug": "availability"}, partial=True)
    assert not serializer.is_valid()
    assert serializer.errors["slug"][0].code == "invalid"
    professional = ProfessionalFactory.create()
    professional.slug = "availability"
    with pytest.raises(ValidationError) as caught:
        professional.clean()
    assert "slug" in caught.value.message_dict
    assert not Professional.objects.filter(slug="availability").exists()


# --- clinics/ ----------------------------------------------------------------------------------------------


def test_clinics_list(public_client: APIClient, now: dict[str, datetime]) -> None:
    first = LocationFactory.create(
        name_uz="Yunusobod", name_ru="Юнусабад", position=1, latitude="41.360000", longitude="69.280000"
    )
    second = LocationFactory.create(name_uz="Chilonzor", position=2, phone="")
    LocationFactory.create(name_uz="Yopiq", is_active=False, position=0)
    specialist(location=first, start=time(9, 0), end=time(18, 0))
    late = specialist(location=first, start=time(10, 0), end=time(19, 30))
    late.professional.additional_locations.add(second)
    ProfessionalFactory.create(location=second, is_active=False)  # a draft is not counted
    response = public_client.get(CLINICS, HTTP_ACCEPT_LANGUAGE="ru")
    assert response.status_code == 200
    assert response["Cache-Control"] == "public, max-age=60"
    rows = response.json()["results"]
    assert [row["id"] for row in rows] == [first.pk, second.pk]
    assert rows[0] == {
        "id": first.pk,
        "name": "Юнусабад",
        "address": first.address_uz,
        "directions": "",
        "phone": first.phone,
        "latitude": 41.36,
        "longitude": 69.28,
        "specialist_count": 2,
        "today_hours": {"start": "09:00", "end": "19:30"},
        "open_now": False,  # 08:00
    }
    assert rows[1]["specialist_count"] == 1
    assert rows[1]["today_hours"] is None and rows[1]["open_now"] is False
    assert rows[1]["phone"] is None
    now["now"] = at(MONDAY, 19, 29)
    assert public_client.get(CLINICS).json()["results"][0]["open_now"] is True
    now["now"] = at(MONDAY, 19, 30)
    assert public_client.get(CLINICS).json()["results"][0]["open_now"] is False


def test_clinics_empty(public_client: APIClient, now: dict[str, datetime]) -> None:
    assert public_client.get(CLINICS).json() == {"results": []}


def test_clinic_hours_follow_the_day_override(public_client: APIClient, now: dict[str, datetime]) -> None:
    service = specialist()
    WorkingHoursOverrideFactory.create(professional=service.professional, date=MONDAY, start=None, end=None)
    row = public_client.get(CLINICS).json()["results"][0]
    assert row["today_hours"] is None and row["specialist_count"] == 1


def test_clinics_query_count_is_bounded(public_client: APIClient, now: dict[str, datetime]) -> None:
    def cost() -> int:
        with CaptureQueriesContext(connection) as captured:
            assert public_client.get(CLINICS).status_code == 200
        return len(captured.captured_queries)

    specialist()
    small = cost()
    for _ in range(3):
        specialist()
    assert cost() == small


# --- the public cache of the catalogue lists ---


@pytest.mark.parametrize("path", ["/api/v1/specialists/", "/api/v1/services/"])
def test_catalogue_lists_are_publicly_cached(public_client: APIClient, path: str) -> None:
    response = public_client.get(path)
    assert response.status_code == 200
    assert response["Cache-Control"] == "public, max-age=60"


def test_selector_clamps_days(now: dict[str, datetime]) -> None:
    specialist()
    assert selectors.specialists_availability(0)[0].slots_today == 16
