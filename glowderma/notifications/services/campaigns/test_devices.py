"""The staff test devices campaign test pushes go to."""

from __future__ import annotations

from typing import Any

from django.db import transaction
from rest_framework.exceptions import NotFound, ValidationError

from glowderma.accounts.models import Customer, Device
from glowderma.core import audit, enums, pii
from glowderma.core.api.exceptions import field_error
from glowderma.core.uzsource import N_
from glowderma.notifications import selectors, state
from glowderma.notifications.models import (
    TestDevice,
)

TEST_DEVICE_MESSAGES = {
    "customer_not_found": N_("Bu raqam bilan mijoz topilmadi."),
    "device_not_pushable": N_("Bu mijozning push qabul qiladigan qurilmasi yo‘q."),
    "unique": N_("Bu qurilma allaqachon ro‘yxatda."),
    "does_not_exist": N_("Test qurilmasi topilmadi."),
}


def _test_device_image(test_device: TestDevice) -> dict[str, Any]:
    """The audit image: the audit log is readable by every staff member, the registry (and the full number of
    the superuser's personal phone) by superusers only, so the number is masked here."""
    return {
        "device_id": test_device.device_id,
        "phone": pii.mask_phone(test_device.phone),
        "label": test_device.label,
        "is_active": test_device.is_active,
    }


@transaction.atomic
def register_test_device(*, phone: str, label: str, actor: Any, request: Any = None) -> TestDevice:
    """``POST notifications/test-devices/`` (superuser): the active customer with ``phone`` must have a
    pushable device (its most recently active one is registered). Field errors ``phone``
    ``customer_not_found`` / ``device_not_pushable`` / ``unique``. Audit ``test_device.registered``."""
    customer = Customer.objects.filter(phone=phone, status=enums.CustomerStatus.ACTIVE).first()
    if customer is None:
        raise ValidationError(
            {"phone": [field_error("customer_not_found", TEST_DEVICE_MESSAGES["customer_not_found"])]}
        )
    device = (
        # The row lock orders two registrations of one device: the second waits and sees the duplicate
        # instead of hitting the partial unique index (``testdevice_active_device_unique``).
        Device.objects.select_for_update()
        .filter(selectors.PUSHABLE_DEVICE, customer=customer)
        .order_by("-last_active_at", "-id")
        .first()
    )
    if device is None:
        raise ValidationError(
            {"phone": [field_error("device_not_pushable", TEST_DEVICE_MESSAGES["device_not_pushable"])]}
        )
    if TestDevice.objects.filter(device=device, is_active=True).exists():
        raise ValidationError({"phone": [field_error("unique", TEST_DEVICE_MESSAGES["unique"])]})
    test_device = TestDevice.objects.create(
        device=device, phone=phone, label=label.strip(), registered_by=actor
    )
    audit.record(
        action=state.AUDIT_TEST_DEVICE_REGISTERED,
        entity_type="test_device",
        entity_id=str(test_device.pk),
        actor=actor,
        request=request,
        after=_test_device_image(test_device),
    )
    return test_device


@transaction.atomic
def remove_test_device(test_device_id: int, *, actor: Any, request: Any = None) -> TestDevice:
    """``DELETE notifications/test-devices/{id}/`` (superuser): soft (``is_active = False``), idempotent
    (an inactive row answers unchanged, no second audit); 404 for an unknown id."""
    test_device = TestDevice.objects.select_for_update().filter(pk=test_device_id).first()
    if test_device is None:
        raise NotFound
    if not test_device.is_active:
        return test_device
    test_device.is_active = False
    test_device.save(update_fields=["is_active"])
    audit.record(
        action=state.AUDIT_TEST_DEVICE_REMOVED,
        entity_type="test_device",
        entity_id=str(test_device.pk),
        actor=actor,
        request=request,
        before={"is_active": True},
        after=_test_device_image(test_device),
    )
    return test_device


__all__ = [
    "TEST_DEVICE_MESSAGES",
    "register_test_device",
    "remove_test_device",
]
