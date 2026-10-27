"""Edges of the specialists admin API closed by the review fix pass: blocks on the 5-minute grid, bounded
schedule list ranges, the export's offers, a kept staff link, the Django admin's deactivation guard and
the photo file cleanup on a rollback."""

import csv
from collections.abc import Callable
from datetime import datetime, time, timedelta
from io import StringIO
from typing import Any

import pytest
from django.core.exceptions import ValidationError as DjangoValidationError
from rest_framework.test import APIClient

from glowderma.accounts.tests.factories import StaffUserFactory
from glowderma.appointments.tests.factories import AppointmentFactory
from glowderma.catalog.tests.helpers import upload
from glowderma.core import audit
from glowderma.core.models import AuditEntry
from glowderma.core.timeutils import TASHKENT
from glowderma.professionals import services
from glowderma.professionals.models import BlockedPeriod, Professional
from glowderma.professionals.tests.conftest import ANCHOR, errors
from glowderma.professionals.tests.factories import (
    ProfessionalFactory,
    ProfessionalServiceFactory,
    ServiceFactory,
    WorkingHoursFactory,
)

pytestmark = [pytest.mark.django_db, pytest.mark.usefixtures("clock")]

BASE = "/api/v1/admin/specialists/"
MONDAY = ANCHOR.date() + timedelta(days=2)  # 26 Oct 2026
GRID = "Vaqt 5 daqiqalik qadamda bo‘lsin."
FAR = "Sana bugundan ko‘pi bilan 10 yil uzoqlikda bo‘lsin."


def url(professional: Professional, suffix: str = "") -> str:
    return f"{BASE}{professional.pk}/{suffix}"


def at(hour: int, minute: int = 0, second: int = 0) -> str:
    return datetime.combine(MONDAY, time(hour, minute, second), tzinfo=TASHKENT).isoformat()


# --- blocks sit on the 5-minute grid ------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("start", "end", "field"),
    [
        (at(10), at(10, 30, 45), "end"),  # the review probe: 10:30 stayed bookable inside the block
        (at(10, 0, 1), at(10, 30), "start"),
        (at(10, 3), at(10, 30), "start"),
        (at(10), at(10, 32), "end"),
        ("2026-10-26T10:00:00.5+05:00", at(10, 30), "start"),
    ],
)
def test_a_block_edge_off_the_grid_is_refused(
    staff_client: APIClient, start: str, end: str, field: str
) -> None:
    professional = ProfessionalFactory.create()
    body = {"start": start, "end": end, "kind": "busy"}
    assert errors(staff_client.post(url(professional, "blocks/"), body, format="json")) == [
        (field, "invalid", GRID)
    ]
    assert not BlockedPeriod.objects.exists()


def test_a_block_on_the_grid_removes_every_overlapping_slot(
    staff_client: APIClient, public_client: APIClient
) -> None:
    professional = ProfessionalFactory.create(is_active=True, slot_step_min=30)
    service = ServiceFactory.create(default_duration_min=30)
    ProfessionalServiceFactory.create(professional=professional, service=service, duration_min=30, price=1)
    WorkingHoursFactory.create(professional=professional, weekday=0, start=time(9), end=time(12))
    body = {"start": at(10), "end": at(10, 35), "kind": "busy"}
    assert staff_client.post(url(professional, "blocks/"), body, format="json").status_code == 201
    response = public_client.get(
        f"/api/v1/specialists/{professional.pk}/slots/", {"date": str(MONDAY), "service_id": str(service.pk)}
    )
    starts = [slot["start"] for slot in response.json()["slots"]]
    assert starts == ["09:00", "09:30", "11:00", "11:30"]


# --- schedule list ranges are bounded -----------------------------------------------------------------------


@pytest.mark.parametrize("suffix", ["overrides/", "blocks/"])
@pytest.mark.parametrize(
    ("query", "field"),
    [
        ({"date_to": "9999-12-31"}, "date_to"),  # day_bounds overflowed: a 500 before the fix
        ({"date_from": "0001-01-01"}, "date_from"),
        ({"date_from": "2036-12-01"}, "date_from"),
    ],
)
def test_a_schedule_range_beyond_ten_years_is_refused(
    staff_client: APIClient, suffix: str, query: dict[str, str], field: str
) -> None:
    professional = ProfessionalFactory.create()
    assert errors(staff_client.get(url(professional, suffix), query)) == [(field, "out_of_range", FAR)]


@pytest.mark.parametrize("suffix", ["overrides/", "blocks/"])
def test_a_schedule_range_within_ten_years_is_listed(staff_client: APIClient, suffix: str) -> None:
    professional = ProfessionalFactory.create()
    response = staff_client.get(
        url(professional, suffix), {"date_from": "2020-01-01", "date_to": "2036-01-01"}
    )
    assert response.status_code == 200, response.json()


# --- the export lists only offers the catalogue offers ------------------------------------------------------


def test_the_export_leaves_out_offers_of_a_retired_service(staff_client: APIClient) -> None:
    professional = ProfessionalFactory.create()
    live = ServiceFactory.create(short_name_uz="Konsultatsiya", position=0)
    retired = ServiceFactory.create(short_name_uz="Pilling", position=1, is_active=False)
    ProfessionalServiceFactory.create(professional=professional, service=live)
    ProfessionalServiceFactory.create(professional=professional, service=retired)
    response = staff_client.get(BASE, {"export": "csv"})
    rows = list(csv.reader(StringIO(response.content.decode("utf-8-sig")), delimiter=";"))
    assert rows[1][4] == "Konsultatsiya"


# --- the staff link already stored is accepted back ---------------------------------------------------------


def test_a_patch_resending_the_linked_staff_account_after_its_deactivation_is_accepted(
    staff_client: APIClient,
) -> None:
    doctor = StaffUserFactory.create()
    professional = ProfessionalFactory.create(staff_user=doctor)
    doctor.is_active = False
    doctor.save(update_fields=["is_active"])
    response = staff_client.patch(
        url(professional), {"name": "Nodira Rahimova", "staff_user_id": doctor.pk}, format="json"
    )
    assert response.status_code == 200, response.json()
    assert response.json()["staff_user"]["id"] == doctor.pk
    # a different inactive account is still refused
    other = StaffUserFactory.create(is_active=False)
    assert errors(staff_client.patch(url(professional), {"staff_user_id": other.pk}, format="json")) == [
        ("staff_user_id", "does_not_exist", "Xodim topilmadi.")
    ]


# --- the Django admin path keeps the deactivation guard (PS-2) ----------------------------------------------


def test_the_django_admin_path_refuses_deactivating_a_specialist_with_appointments_ahead() -> None:
    staff = StaffUserFactory.create()
    professional = ProfessionalFactory.create(is_active=True)
    AppointmentFactory.create(
        professional=professional, start=datetime.combine(MONDAY, time(10), tzinfo=TASHKENT)
    )
    professional.is_active = False
    with pytest.raises(DjangoValidationError) as caught:
        services.save_row(professional, actor=staff)
    assert "1 ta kelgusi qabul" in str(caught.value.message_dict["is_active"])
    assert Professional.objects.get(pk=professional.pk).is_active is True
    assert not AuditEntry.objects.exists()
    idle = ProfessionalFactory.create(is_active=True)
    idle.is_active = False
    services.save_row(idle, actor=staff)
    assert Professional.objects.get(pk=idle.pk).is_active is False


# --- a failed photo write leaves no file behind -------------------------------------------------------------


def test_a_photo_write_that_rolls_back_removes_the_new_file(
    monkeypatch: pytest.MonkeyPatch, django_capture_on_commit_callbacks: Callable[..., Any]
) -> None:
    staff = StaffUserFactory.create()
    professional = ProfessionalFactory.create()
    written: list[str] = []
    record = audit.record

    def failing(**kwargs: Any) -> Any:
        written.append(Professional.objects.get(pk=professional.pk).photo.name or "")
        raise RuntimeError("audit down")

    monkeypatch.setattr(audit, "record", failing)
    with pytest.raises(RuntimeError):
        services.set_specialist_photo(professional.pk, upload("PNG"), actor=staff)
    storage = Professional._meta.get_field("photo").storage
    assert written and written[0] and not storage.exists(written[0])
    assert Professional.objects.get(pk=professional.pk).photo.name == ""
    monkeypatch.setattr(audit, "record", record)
    with django_capture_on_commit_callbacks(execute=True):
        stored = services.set_specialist_photo(professional.pk, upload("PNG"), actor=staff)
    assert storage.exists(stored.photo.name or "")
