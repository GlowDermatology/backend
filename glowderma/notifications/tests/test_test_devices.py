"""The test-device registry (admin contract §2.8 ``notifications/test-devices/``, superuser): register by the
customer's phone, the refusals, the soft removal and a dead token pruning the device."""

from typing import Any

import pytest
from rest_framework.test import APIClient

from glowderma.accounts.models import Device, StaffUser
from glowderma.accounts.tests.factories import CustomerFactory, DeviceFactory
from glowderma.core.models import AuditEntry
from glowderma.integrations.fcm.fake import FakeFcm
from glowderma.notifications import services
from glowderma.notifications.models import TestDevice
from glowderma.notifications.tests.conftest import ANCHOR
from glowderma.notifications.tests.factories import CampaignFactory, TestDeviceFactory

pytestmark = pytest.mark.django_db

URL = "/api/v1/admin/notifications/test-devices/"


def errors(response: Any) -> list[tuple[str | None, str, str]]:
    return [(e["field"], e["code"], e["message"]) for e in response.data["errors"]]


def register(client: APIClient, phone: str, label: str = "Dilnoza — iPhone") -> Any:
    return client.post(URL, {"phone": phone, "label": label}, format="json")


def test_register_by_phone_lists_and_audits(
    superuser_client: APIClient, superuser: StaffUser, clock: Any
) -> None:
    customer = CustomerFactory.create(phone="+998901234567", name="Dilnoza")
    DeviceFactory.create(customer=customer, last_active_at=ANCHOR.replace(day=20))
    latest = DeviceFactory.create(customer=customer, model="iPhone 15", last_active_at=ANCHOR)
    DeviceFactory.create(customer=customer, push_enabled=False)  # not pushable: never chosen
    response = register(superuser_client, "+998901234567", "  Dilnoza — iPhone  ")
    assert response.status_code == 201, response.data
    assert response.data == {
        "id": response.data["id"],
        "phone": "+998901234567",
        "label": "Dilnoza — iPhone",
        "device_id": latest.pk,
        "platform": "ios",
        "model": "iPhone 15",
        "is_pushable": True,
        "registered_by": {"id": superuser.pk, "display_name": "Egasi"},
        "created_at": ANCHOR.isoformat(),
    }
    entry = AuditEntry.objects.get(action="test_device.registered")
    assert entry.actor_id == superuser.pk and entry.entity_id == str(response.data["id"])
    assert entry.after == {
        "device_id": latest.pk,
        "phone": "+998901234567",
        "label": "Dilnoza — iPhone",
        "is_active": True,
    }
    listed = superuser_client.get(URL)
    assert listed.status_code == 200 and [row["id"] for row in listed.data] == [response.data["id"]]
    # a second customer: the registry is global
    other = CustomerFactory.create(phone="+998901234568")
    DeviceFactory.create(customer=other)
    assert register(superuser_client, other.phone or "", "Ikkinchi").status_code == 201
    assert len(superuser_client.get(URL).data) == 2


@pytest.mark.parametrize(
    ("phone", "label", "expected"),
    [
        ("901234567", "Telefon", [("phone", "invalid", "Telefon raqamini to‘g‘ri kiriting.")]),
        ("+998900000009", "Telefon", [("phone", "customer_not_found", "Bu raqam bilan mijoz topilmadi.")]),
        ("+998901234567", "", [("label", "min_length", "Qurilma nomini kiriting.")]),
        ("+998901234567", "   ", [("label", "min_length", "Qurilma nomini kiriting.")]),
        ("+998901234567", "x" * 61, [("label", "max_length", "60 belgidan oshmasin.")]),
    ],
)
def test_register_validation(
    superuser_client: APIClient, phone: str, label: str, expected: list[Any], clock: Any
) -> None:
    customer = CustomerFactory.create(phone="+998901234567")
    DeviceFactory.create(customer=customer)
    response = register(superuser_client, phone, label)
    assert response.status_code == 400 and errors(response) == expected
    assert not TestDevice.objects.exists()
    assert errors(superuser_client.post(URL, {}, format="json")) == [
        ("phone", "required", "Majburiy maydon."),
        ("label", "required", "Majburiy maydon."),
    ]


def test_register_refuses_a_customer_without_a_pushable_device_a_deleted_one_and_a_duplicate(
    superuser_client: APIClient, clock: Any
) -> None:
    silent = CustomerFactory.create(phone="+998901234567")
    DeviceFactory.create(customer=silent, push_enabled=False)
    DeviceFactory.create(customer=silent, fcm_token="", push_enabled=False)
    DeviceFactory.create(customer=silent, is_active=False)
    response = register(superuser_client, silent.phone or "")
    assert errors(response) == [
        ("phone", "device_not_pushable", "Bu mijozning push qabul qiladigan qurilmasi yo‘q.")
    ]
    gone = CustomerFactory.create(phone="+998901234568", status="deleted", deleted_at=ANCHOR)
    DeviceFactory.create(customer=gone)
    assert errors(register(superuser_client, gone.phone or ""))[0][1] == "customer_not_found"
    ready = CustomerFactory.create(phone="+998901234569")
    DeviceFactory.create(customer=ready)
    assert register(superuser_client, ready.phone or "").status_code == 201
    duplicate = register(superuser_client, ready.phone or "", "Yana bir nom")
    assert errors(duplicate) == [("phone", "unique", "Bu qurilma allaqachon ro‘yxatda.")]
    assert TestDevice.objects.count() == 1


def test_remove_is_soft_and_idempotent(superuser_client: APIClient, superuser: StaffUser, clock: Any) -> None:
    test_device = TestDeviceFactory.create()
    response = superuser_client.delete(f"{URL}{test_device.pk}/")
    assert response.status_code == 204
    test_device.refresh_from_db()
    assert not test_device.is_active and Device.objects.filter(pk=test_device.device_id).exists()
    assert superuser_client.get(URL).data == []
    entry = AuditEntry.objects.get(action="test_device.removed")
    assert entry.actor_id == superuser.pk and entry.after is not None and entry.after["is_active"] is False
    assert superuser_client.delete(f"{URL}{test_device.pk}/").status_code == 204  # idempotent
    assert AuditEntry.objects.filter(action="test_device.removed").count() == 1
    assert superuser_client.delete(f"{URL}999999/").status_code == 404
    # the device can be registered again (the unique constraint is partial on is_active)
    again = register(superuser_client, test_device.phone, "Qayta")
    assert again.status_code == 201 and again.data["device_id"] == test_device.device_id


def test_a_dead_token_on_a_test_push_prunes_the_device_and_the_registry_shows_it(
    superuser_client: APIClient, superuser: StaffUser, clock: Any, fcm: FakeFcm
) -> None:
    test_device = TestDeviceFactory.create(device=DeviceFactory.create(fcm_token="tok-bad"))  # noqa: S106
    campaign = CampaignFactory.create()
    assert superuser_client.get(URL).data[0]["is_pushable"] is True
    services.send_test(campaign.pk, [test_device.pk], actor=superuser)
    device = Device.objects.get(pk=test_device.device_id)
    assert device.fcm_token == "" and not device.push_enabled and device.fcm_invalidated_at == ANCHOR
    listed = superuser_client.get(URL).data
    assert listed[0]["is_pushable"] is False  # still listed: the superuser sees why the test failed
    assert fcm.sent == []
