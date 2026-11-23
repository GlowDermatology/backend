"""The two namespaces of second factors (the ops console and the admin panel) and the stored device names that
separate them; a scope can only see, use or delete the devices of its own names."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from django_otp.plugins.otp_static.models import StaticDevice, StaticToken
from django_otp.plugins.otp_totp.models import TOTPDevice

from glowderma.accounts.models import StaffUser

# Stored in the device rows (``Device.name``): plain Uzbek, never translated.
DEVICE_NAME = "Ops konsol"
RECOVERY_DEVICE_NAME = "Zaxira kodlar"
# The admin panel's namespace (feat/admin-ux): stored device names, plain Uzbek, never translated.
PANEL_DEVICE_NAME = "Admin panel"
PANEL_RECOVERY_DEVICE_NAME = "Admin panel zaxira kodlar"
PANEL_ISSUER = "Glow Derma Admin"


@dataclass(frozen=True)
class DeviceScope:
    """One namespace of TOTP devices and recovery codes (module doc). ``exclusive`` scopes own exactly the
    devices of their names; the console's (not exclusive) owns every device outside the exclusive names.
    ``issuer`` names the authenticator entry; empty means django-otp's ``OTP_TOTP_ISSUER``."""

    key: str
    totp_name: str
    recovery_name: str
    issuer: str
    exclusive: bool

    def totp_devices(self, user: StaffUser) -> Any:
        rows = TOTPDevice.objects.filter(user=user)
        if self.exclusive:
            return rows.filter(name=self.totp_name)
        return rows.exclude(name__in=EXCLUSIVE_TOTP_NAMES)

    def recovery_devices(self, user: StaffUser) -> Any:
        rows = StaticDevice.objects.filter(user=user)
        if self.exclusive:
            return rows.filter(name=self.recovery_name)
        return rows.exclude(name__in=EXCLUSIVE_RECOVERY_NAMES)

    def recovery_tokens(self, user: StaffUser) -> Any:
        rows = StaticToken.objects.filter(device__user=user)
        if self.exclusive:
            return rows.filter(device__name=self.recovery_name)
        return rows.exclude(device__name__in=EXCLUSIVE_RECOVERY_NAMES)


EXCLUSIVE_TOTP_NAMES = (PANEL_DEVICE_NAME,)
EXCLUSIVE_RECOVERY_NAMES = (PANEL_RECOVERY_DEVICE_NAME,)
CONSOLE = DeviceScope("console", DEVICE_NAME, RECOVERY_DEVICE_NAME, issuer="", exclusive=False)
PANEL = DeviceScope("panel", PANEL_DEVICE_NAME, PANEL_RECOVERY_DEVICE_NAME, PANEL_ISSUER, exclusive=True)


__all__ = [
    "CONSOLE",
    "DEVICE_NAME",
    "EXCLUSIVE_RECOVERY_NAMES",
    "EXCLUSIVE_TOTP_NAMES",
    "PANEL",
    "PANEL_DEVICE_NAME",
    "PANEL_ISSUER",
    "PANEL_RECOVERY_DEVICE_NAME",
    "RECOVERY_DEVICE_NAME",
    "DeviceScope",
]
