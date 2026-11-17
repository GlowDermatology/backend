"""TOTP second factors: one TOTP device per staff member and *scope* plus ten one-time recovery codes.

Two scopes share these functions (feat/admin-ux) and never see each other's rows: :data:`CONSOLE`, the ops
console's factor (enrolled inside a manager-opened window, ``glowderma.accounts.console.access``), and
:data:`PANEL`, the admin panel's optional TOTP (``glowderma.accounts.mfa``, each person's own choice). A
scope is a namespace of ``Device.name``: the panel's devices are named :data:`PANEL_DEVICE_NAME` /
:data:`PANEL_RECOVERY_DEVICE_NAME`; the console's are every other device (named :data:`DEVICE_NAME` /
:data:`RECOVERY_DEVICE_NAME` since enrolment began, so a console row from before the panel scope existed
keeps working). Every helper takes ``scope`` (keyword-only, required): the panel can never confirm, use,
count or delete a console device, and the console never accepts a panel device or recovery code, so the
console's enrolment window and four-eyes rule cannot be bypassed from the panel.

TOTP (django-otp ``TOTPDevice``): 30 s steps, 6 digits, ±1 step of tolerance. A verified code moves the
device's ``last_t`` forward, so the same code (or an older one) is refused afterwards: no replay inside its
window. Enrolment creates an unconfirmed device whose secret is shown once (QR + base32); the first valid code
confirms it, and only a confirmed device ever verifies a session (``OTPMiddleware`` ignores the others).

Recovery codes (django-otp ``StaticDevice``/``StaticToken``): otp_static stores its tokens in plain text and
``StaticDevice.verify_token`` compares them as stored, so this module never uses that method. A
``StaticToken.token`` holds ``recovery_hash(code)`` instead — HMAC-SHA256 keyed by ``SECRET_KEY`` (salted
for this purpose), base32, the first 16 characters (80 bits: the column is ``max_length=16``) — and a code is
checked by recomputing that hash (also under each ``SECRET_KEY_FALLBACKS`` key, so a key rotation keeps the
codes) and deleting the matching row. No schema change: a database dump alone reveals no usable code, and the
stock ``addstatictoken`` command's plain tokens never match a hash. Codes are ten base32 characters (50
bits), shown once as ``xxxxx-xxxxx``; the code step's lockout bounds online guessing.

Accepted risk: the TOTP seed (``TOTPDevice.key``) stays in plain text — django-otp reads the column directly
to compute codes, and encrypting it would mean forking its device model. A database dump plus a known password
can therefore produce codes; after a suspected leak every console user gets ``ops_access reset-2fa``
(docs/ops/staff-access.md "Accepted risks").
"""

from __future__ import annotations

import base64
import re
import secrets
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote, urlencode

import segno
from django.conf import settings
from django.db import transaction
from django.utils.crypto import salted_hmac
from django.utils.safestring import SafeString, mark_safe
from django.utils.translation import gettext
from django_otp import verify_token
from django_otp.plugins.otp_static.models import StaticDevice, StaticToken
from django_otp.plugins.otp_totp.models import TOTPDevice

from glowderma.accounts.models import StaffUser

# Stored in the device rows (``Device.name``): plain Uzbek, never translated.
DEVICE_NAME = "Ops konsol"
RECOVERY_DEVICE_NAME = "Zaxira kodlar"
RECOVERY_CODE_COUNT = 10
RECOVERY_ALPHABET = "abcdefghijklmnopqrstuvwxyz234567"
RECOVERY_LENGTH = 10
RECOVERY_SALT = "glowderma.accounts.console.recovery-code"
TOTP_CODE = re.compile(r"^\d{6}$")
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


def confirmed_totp(user: StaffUser, *, scope: DeviceScope) -> TOTPDevice | None:
    return scope.totp_devices(user).filter(confirmed=True).order_by("pk").first()


def has_second_factor(user: StaffUser, *, scope: DeviceScope) -> bool:
    return confirmed_totp(user, scope=scope) is not None


# --- TOTP enrolment and verification -------------------------------------------------------------------


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
        codes = issue_recovery_codes(user, scope=scope)
    return Enrolment(device, codes)


def verify_totp(user: StaffUser, token: str, *, scope: DeviceScope) -> TOTPDevice | None:
    """The user's confirmed device in ``scope`` when ``token`` is its current code (not used before: the
    device row is locked and ``last_t`` moves forward), else ``None``."""
    device = confirmed_totp(user, scope=scope)
    if device is None or not TOTP_CODE.match(token):
        return None
    verified = verify_token(user, device.persistent_id, token)
    return verified if isinstance(verified, TOTPDevice) else None


def manual_key(device: TOTPDevice) -> str:
    """The secret in base32 (what authenticator apps accept when typed), in groups of four."""
    encoded = base64.b32encode(device.bin_key).decode().rstrip("=")
    return " ".join(encoded[index : index + 4] for index in range(0, len(encoded), 4))


def qr_svg(device: TOTPDevice) -> SafeString:
    """The ``otpauth://`` URI as an inline SVG QR code (no image request, no external service)."""
    code = segno.make(device.config_url, error="m")
    svg = code.svg_inline(
        scale=5, border=3, dark="#000000", light="#ffffff", svgclass="ops-qr", title=gettext("TOTP QR kod")
    )
    return mark_safe(svg)  # noqa: S308 - generated by segno from our own URI; no user markup


def otpauth_uri(device: TOTPDevice, *, scope: DeviceScope) -> str:
    """The ``otpauth://totp/`` URI of ``device`` under the scope's issuer (django-otp's ``config_url``
    format, built here so the panel's entry reads "Glow Derma Admin" while ``OTP_TOTP_ISSUER`` stays the
    console's)."""
    if not scope.issuer:
        return str(device.config_url)
    issuer = scope.issuer.replace(":", "")
    label = f"{issuer}:{device.user.get_username()}"
    params = urlencode(
        {
            "secret": base64.b32encode(device.bin_key).decode(),
            "algorithm": "SHA1",
            "digits": device.digits,
            "period": device.step,
        }
    )
    return f"otpauth://totp/{quote(label)}?{params}&issuer={quote(issuer)}"


# --- Recovery codes ------------------------------------------------------------------------------------


def normalize_recovery_code(code: str) -> str:
    return re.sub(r"[^a-z0-9]", "", code.strip().lower())


def recovery_hash(code: str, secret: str | bytes | None = None) -> str:
    digest = salted_hmac(
        RECOVERY_SALT, normalize_recovery_code(code), secret=secret, algorithm="sha256"
    ).digest()
    return base64.b32encode(digest).decode()[:16]


def _candidate_hashes(code: str) -> list[str]:
    keys = [settings.SECRET_KEY, *getattr(settings, "SECRET_KEY_FALLBACKS", [])]
    return [recovery_hash(code, key) for key in keys]


def issue_recovery_codes(user: StaffUser, *, scope: DeviceScope) -> list[str]:
    """Replace the user's recovery codes in ``scope`` with ten new ones; the plain codes are returned once,
    only their keyed hashes are stored."""
    codes = [
        "".join(secrets.choice(RECOVERY_ALPHABET) for _ in range(RECOVERY_LENGTH))
        for _ in range(RECOVERY_CODE_COUNT)
    ]
    with transaction.atomic():
        scope.recovery_devices(user).delete()
        device = StaticDevice.objects.create(user=user, name=scope.recovery_name, confirmed=True)
        StaticToken.objects.bulk_create(
            StaticToken(device=device, token=recovery_hash(code)) for code in codes
        )
    half = RECOVERY_LENGTH // 2
    return [f"{code[:half]}-{code[half:]}" for code in codes]


@dataclass(frozen=True)
class RecoveryUse:
    device: StaticDevice
    remaining: int


def use_recovery_code(user: StaffUser, code: str, *, scope: DeviceScope) -> RecoveryUse | None:
    """Consume one recovery code of ``user`` in ``scope`` (the row is deleted: a code works once); ``None`` if
    no unused code of the scope matches."""
    normalized = normalize_recovery_code(code)
    if len(normalized) != RECOVERY_LENGTH or not set(normalized) <= set(RECOVERY_ALPHABET):
        return None
    with transaction.atomic():
        token = (
            scope.recovery_tokens(user)
            .select_for_update()
            .select_related("device")
            .filter(device__confirmed=True, token__in=_candidate_hashes(normalized))
            .first()
        )
        if token is None:
            return None
        device = token.device
        token.delete()
        device.set_last_used_timestamp(commit=True)
        remaining = scope.recovery_tokens(user).filter(device__confirmed=True).count()
    return RecoveryUse(device, remaining)


def recovery_codes_left(user: StaffUser, *, scope: DeviceScope) -> int:
    return int(scope.recovery_tokens(user).filter(device__confirmed=True).count())


# --- Reset -----------------------------------------------------------------------------------------------


def reset_devices(user: StaffUser, *, scope: DeviceScope) -> dict[str, int]:
    """Delete every TOTP device and recovery code of ``user`` in ``scope``: the next login enrols again, and
    a console session verified by a deleted device stops being verified at once (``OTPMiddleware`` finds no
    device). The other scope's devices stay."""
    with transaction.atomic():
        totp, _ = scope.totp_devices(user).delete()
        codes = scope.recovery_tokens(user).count()
        scope.recovery_devices(user).delete()
    return {"totp_devices": totp, "recovery_codes": codes}
