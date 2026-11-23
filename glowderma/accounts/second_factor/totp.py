"""TOTP devices: enrolment (an unconfirmed device confirmed by its first code), and verification of a code
against the confirmed device of a scope (no replay: ``last_t`` moves forward)."""

from __future__ import annotations

import re
from dataclasses import dataclass

from django.db import transaction
from django_otp import verify_token
from django_otp.plugins.otp_totp.models import TOTPDevice

from glowderma.accounts.models import StaffUser
from glowderma.accounts.second_factor import recovery_codes
from glowderma.accounts.second_factor.scopes import DeviceScope

TOTP_CODE = re.compile(r"^\d{6}$")


def confirmed_totp(user: StaffUser, *, scope: DeviceScope) -> TOTPDevice | None:
    return scope.totp_devices(user).filter(confirmed=True).order_by("pk").first()


def has_second_factor(user: StaffUser, *, scope: DeviceScope) -> bool:
    return confirmed_totp(user, scope=scope) is not None


def enrolment_device(user: StaffUser, device_id: int | None, *, scope: DeviceScope) -> TOTPDevice:
    """The unconfirmed device of the enrolment in progress (``device_id`` from the session), or a new one;
    any other unconfirmed device of the user in ``scope`` is removed, so one secret is pending at a time."""
    device = None
    if device_id is not None:
        device = scope.totp_devices(user).filter(pk=device_id, confirmed=False).first()
    if device is None:
        scope.totp_devices(user).filter(confirmed=False).delete()
        device = TOTPDevice.objects.create(
            user=user, name=scope.totp_name, confirmed=False, step=30, tolerance=1, digits=6
        )
    return device


@dataclass(frozen=True)
class Enrolment:
    device: TOTPDevice
    recovery_codes: list[str]


def confirm_enrolment(user: StaffUser, device_id: int, token: str, *, scope: DeviceScope) -> Enrolment | None:
    """Confirm the pending device with its first code and issue the recovery codes; ``None`` when the code is
    wrong, the device is gone, or the user meanwhile has a confirmed device in ``scope`` (a second, parallel
    enrolment). The scope's other TOTP devices are removed; another scope's stay."""
    if not TOTP_CODE.match(token):
        return None
    with transaction.atomic():
        StaffUser.objects.select_for_update().filter(pk=user.pk).first()  # one enrolment at a time per user
        if scope.totp_devices(user).filter(confirmed=True).exists():
            return None
        device = scope.totp_devices(user).select_for_update().filter(pk=device_id, confirmed=False).first()
        if device is None or not device.verify_token(token):
            return None
        device.confirmed = True
        device.save(update_fields=["confirmed"])
        scope.totp_devices(user).exclude(pk=device.pk).delete()
        codes = recovery_codes.issue_recovery_codes(user, scope=scope)
    return Enrolment(device, codes)


def verify_totp(user: StaffUser, token: str, *, scope: DeviceScope) -> TOTPDevice | None:
    """The user's confirmed device in ``scope`` when ``token`` is its current code (not used before: the
    device row is locked and ``last_t`` moves forward), else ``None``."""
    device = confirmed_totp(user, scope=scope)
    if device is None or not TOTP_CODE.match(token):
        return None
    verified = verify_token(user, device.persistent_id, token)
    return verified if isinstance(verified, TOTPDevice) else None


__all__ = [
    "TOTP_CODE",
    "Enrolment",
    "confirm_enrolment",
    "confirmed_totp",
    "enrolment_device",
    "has_second_factor",
    "verify_totp",
]
