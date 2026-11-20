"""``PATCH clinics/{id}/``: the clinic phone accepts the spellings staff paste and is stored as E.164
(feat/admin-ux, ``core.api.fields.LenientPhoneField``)."""

import pytest
from rest_framework.test import APIClient

from glowderma.core.models import AuditEntry
from glowderma.professionals.models import Location
from glowderma.professionals.tests.conftest import errors
from glowderma.professionals.tests.factories import LocationFactory

pytestmark = [pytest.mark.django_db, pytest.mark.usefixtures("clock")]


def test_location_phone_is_normalised(staff_client: APIClient) -> None:
    location = LocationFactory.create(name_uz="Klinika", phone="+998712000000")
    url = f"/api/v1/admin/clinics/{location.pk}/"
    response = staff_client.patch(url, {"phone": "(71) 200-00-01"})
    assert response.status_code == 200, response.json()
    assert response.json()["phone"] == "+998712000001"
    location.refresh_from_db()
    assert location.phone == "+998712000001"
    # the same number in another spelling changes nothing and writes no audit row
    assert staff_client.patch(url, {"phone": "998 71 200 00 01"}).status_code == 200
    assert AuditEntry.objects.filter(action="location.updated").count() == 1
    assert errors(staff_client.patch(url, {"phone": "71 200 00"})) == [
        ("phone", "invalid", "Telefon raqamini to‘g‘ri kiriting.")
    ]
    assert Location.objects.get().phone == "+998712000001"
