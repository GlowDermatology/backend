"""Clinics can be created and edited independently, with audited localized fields."""

from decimal import Decimal
from typing import Any

import pytest
from rest_framework.test import APIClient

from glowderma.accounts.models import StaffUser
from glowderma.core import timeutils
from glowderma.core.models import AuditEntry
from glowderma.professionals.models import Location, Professional
from glowderma.professionals.tests.conftest import ANCHOR, errors
from glowderma.professionals.tests.factories import LocationFactory

pytestmark = [pytest.mark.django_db, pytest.mark.usefixtures("clock")]

URL = "/api/v1/admin/clinics/"


def test_staff_create_and_retrieve_a_clinic(staff_client: APIClient, staff: StaffUser) -> None:
    assert staff_client.get(URL).json() == {"results": []}
    assert staff_client.get(URL + "999999/").status_code == 404
    assert staff_client.post(URL, {"phone": "+998712000000"}).status_code == 400
    response = staff_client.post(
        URL,
        {
            "name": {"uz": "Glow Derma klinikasi", "ru": "Клиника Glow Derma"},
            "address": {"uz": "Toshkent, Chilonzor"},
            "phone": "+998712000000",
            "latitude": "41.311081",
            "longitude": 69.240562,
        },
    )
    assert response.status_code == 201, response.json()
    location = Location.objects.get()
    assert response.json() == {
        "id": location.pk,
        "name": {"uz": "Glow Derma klinikasi", "ru": "Клиника Glow Derma", "en": ""},
        "address": {"uz": "Toshkent, Chilonzor", "ru": "", "en": ""},
        "directions": {"uz": "", "ru": "", "en": ""},
        "is_active": True,
        "phone": "+998712000000",
        "latitude": 41.311081,
        "longitude": 69.240562,
        "updated_at": timeutils.isoformat(ANCHOR),
    }
    assert (location.latitude, location.longitude) == (Decimal("41.311081"), Decimal("69.240562"))
    [entry] = AuditEntry.objects.filter(action="location.created")
    assert (entry.entity_type, entry.entity_id, entry.actor_id) == ("location", str(location.pk), staff.pk)
    assert entry.before is None and entry.after is not None and entry.after["latitude"] == "41.311081"
    assert staff_client.get(f"{URL}{location.pk}/").json() == response.json()
    assert staff_client.get(URL).json() == {"results": [response.json()]}
    created = staff_client.post(
        "/api/v1/admin/specialists/",
        {
            "name": "Nodira",
            "role": "dermatologist",
            "location_id": location.pk,
            "location_ids": [location.pk],
        },
    )
    assert created.json()["location_id"] == location.pk == Professional.objects.get().location_id


def test_patch_merges_and_audits_the_changed_leaves(staff_client: APIClient) -> None:
    location = LocationFactory.create(name_uz="Klinika", address_uz="Eski manzil", phone="+998712000000")
    other = LocationFactory.create(name_uz="Ikkinchi", position=5)
    url = f"{URL}{location.pk}/"
    response = staff_client.patch(url, {"address": {"ru": "Новый адрес"}, "phone": None})
    assert response.status_code == 200, response.json()
    body = response.json()
    assert body["id"] == location.pk and body["address"] == {
        "uz": "Eski manzil",
        "ru": "Новый адрес",
        "en": "",
    }
    assert body["phone"] is None
    assert staff_client.get(f"{URL}{other.pk}/").json()["address"]["ru"] == ""
    [entry] = AuditEntry.objects.filter(action="location.updated")
    assert entry.before == {"address.ru": "", "phone": "+998712000000"}
    assert entry.after == {"address.ru": "Новый адрес", "phone": None}
    assert staff_client.patch(url, {"phone": None}).status_code == 200  # nothing changed: no audit
    assert AuditEntry.objects.filter(action="location.updated").count() == 1


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ({"latitude": "41.3"}, ("longitude", "latlng_pair", "Kenglik va uzunlikni birga kiriting.")),
        ({"latitude": "91", "longitude": "69"}, ("latitude", "max_value", "Qiymat 90 dan oshmasin.")),
        (
            {"latitude": "41.1234567", "longitude": "69"},
            ("latitude", "max_decimal_places", "Ko‘pi bilan 6 ta kasr raqam."),
        ),
        (
            {"longitude": "-181", "latitude": "0"},
            ("longitude", "min_value", "Qiymat -180 dan kam bo‘lmasin."),
        ),
        ({"phone": "99871200000"}, ("phone", "invalid", "Telefon raqamini to‘g‘ri kiriting.")),
        ({"name": {"uz": "K"}}, ("name.uz", "min_length", "Kamida 2 ta belgi kiriting.")),
        ({"address": {"uz": "x" * 301}}, ("address.uz", "max_length", "300 belgidan oshmasin.")),
    ],
)
def test_patch_validation(
    staff_client: APIClient, body: dict[str, Any], expected: tuple[str, str, str]
) -> None:
    location = LocationFactory.create(name_uz="Klinika", phone="")
    url = f"{URL}{location.pk}/"
    assert errors(staff_client.patch(url, body)) == [expected]
    assert not AuditEntry.objects.exists()


def test_clearing_one_coordinate_needs_the_other(staff_client: APIClient) -> None:
    location = LocationFactory.create(latitude=Decimal("41.3"), longitude=Decimal("69.2"))
    url = f"{URL}{location.pk}/"
    assert errors(staff_client.patch(url, {"latitude": None})) == [
        ("latitude", "latlng_pair", "Kenglik va uzunlikni birga kiriting.")
    ]
    response = staff_client.patch(url, {"latitude": None, "longitude": None})
    assert response.status_code == 200 and response.json()["latitude"] is None
