"""Admin phone leniency (feat/admin-ux, ``core.api.fields.LenientPhoneField``) on the notifications admin API:
segment members ``phones``, the one-customer audience ``audience.phone`` and the test-device registry. The
stored value is E.164 whatever spelling staff pasted."""

from typing import Any

import pytest
from rest_framework.test import APIClient

from glowderma.accounts.tests.factories import CustomerFactory, DeviceFactory
from glowderma.notifications.models import Campaign, SegmentMember, TestDevice
from glowderma.notifications.tests.factories import CustomerSegmentFactory

pytestmark = pytest.mark.django_db

KEY = "6f1c2a0e-9b8d-4c37-8a1e-0123456789cd"


def errors(response: Any) -> list[tuple[str | None, str, str]]:
    return [(e["field"], e["code"], e["message"]) for e in response.data["errors"]]


def test_segment_members_accept_pasted_spellings_and_collapse_them(staff_client: APIClient) -> None:
    segment = CustomerSegmentFactory.create()
    malika = CustomerFactory.create(phone="+998901112233")
    jasur = CustomerFactory.create(phone="+998935556677")
    response = staff_client.post(
        f"/api/v1/admin/segments/{segment.pk}/members/",
        {"phones": ["+998 90 111-22-33", "998901112233", "(93) 555 66 77", "90 111 22 33"]},
        format="json",
    )
    assert response.status_code == 200, response.data
    assert response.data["manual_member_count"] == 2
    assert set(SegmentMember.objects.values_list("customer_id", flat=True)) == {malika.pk, jasur.pk}
    unknown = staff_client.post(
        f"/api/v1/admin/segments/{segment.pk}/members/",
        {"phones": ["90 111 22 33", "90 999 99 99", "abc"]},
        format="json",
    )
    assert errors(unknown) == [("phones.2", "invalid", "Telefon raqamini to‘g‘ri kiriting.")]
    missing = staff_client.post(
        f"/api/v1/admin/segments/{segment.pk}/members/", {"phones": ["90 999 99 99"]}, format="json"
    )
    assert errors(missing) == [("phones.0", "customer_not_found", "Bu raqam bilan mijoz topilmadi.")]


def test_one_customer_audience_accepts_a_national_number(staff_client: APIClient, clock: Any) -> None:
    customer = CustomerFactory.create(phone="+998901234567")
    response = staff_client.post(
        "/api/v1/admin/notifications/",
        {
            "kind": "general",
            "title": "Sizga xabar",
            "body": "Buyurtmangiz tayyor, olib ketishingiz mumkin.",
            "audience.type": "customer",
            "audience.phone": "90 123 45 67",
        },
        format="multipart",
        HTTP_IDEMPOTENCY_KEY=KEY,
    )
    assert response.status_code == 201, response.data
    assert response.data["audience"]["phone"] == customer.phone
    assert Campaign.objects.get(pk=response.data["id"]).audience_phone == "+998901234567"


def test_test_device_registry_accepts_a_pasted_number(superuser_client: APIClient, clock: Any) -> None:
    customer = CustomerFactory.create(phone="+998901234567")
    DeviceFactory.create(customer=customer)
    response = superuser_client.post(
        "/api/v1/admin/notifications/test-devices/",
        {"phone": "+998 (90) 123-45-67", "label": "Sinov telefoni"},
        format="json",
    )
    assert response.status_code == 201, response.data
    assert response.data["phone"] == "+998901234567"
    assert TestDevice.objects.get(pk=response.data["id"]).phone == "+998901234567"
    refused = superuser_client.post(
        "/api/v1/admin/notifications/test-devices/",
        {"phone": "+7 901 234 56 78", "label": "Sinov telefoni"},
        format="json",
    )
    assert errors(refused) == [("phone", "invalid", "Telefon raqamini to‘g‘ri kiriting.")]
