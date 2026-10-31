"""The console's second factor: one TOTP device per staff member plus ten one-time recovery codes.

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

import segno
from django.conf import settings
from django.db import transaction
from django.utils.crypto import salted_hmac
from django.utils.safestring import SafeString, mark_safe
from django_otp import verify_token
from django_otp.plugins.otp_static.models import StaticDevice, StaticToken
from django_otp.plugins.otp_totp.models import TOTPDevice

from glowderma.accounts.models import StaffUser

DEVICE_NAME = "Ops konsol"
RECOVERY_DEVICE_NAME = "Zaxira kodlar"
RECOVERY_CODE_COUNT = 10
RECOVERY_ALPHABET = "abcdefghijklmnopqrstuvwxyz234567"
RECOVERY_LENGTH = 10
RECOVERY_SALT = "glowderma.accounts.console.recovery-code"
TOTP_CODE = re.compile(r"^\d{6}$")


def confirmed_totp(user: StaffUser) -> TOTPDevice | None:
    return TOTPDevice.objects.filter(user=user, confirmed=True).order_by("pk").first()


def has_second_factor(user: StaffUser) -> bool:
    return confirmed_totp(user) is not None


# --- TOTP enrolment and verification -------------------------------------------------------------------


def enrolment_device(user: StaffUser, device_id: int | None) -> TOTPDevice:
    """The unconfirmed device of the enrolment in progress (``device_id`` from the session), or a new one;
    any other unconfirmed device of the user is removed, so one secret is pending at a time."""
    device = None
    if device_id is not None:
        device = TOTPDevice.objects.filter(pk=device_id, user=user, confirmed=False).first()
    if device is None:
        TOTPDevice.objects.filter(user=user, confirmed=False).delete()
        device = TOTPDevice.objects.create(
            user=user, name=DEVICE_NAME, confirmed=False, step=30, tolerance=1, digits=6
        )
    return device


@dataclass(frozen=True)
class Enrolment:
    device: TOTPDevice
    recovery_codes: list[str]


def confirm_enrolment(user: StaffUser, device_id: int, token: str) -> Enrolment | None:
    """Confirm the pending device with its first code and issue the recovery codes; ``None`` when the code is
    wrong, the device is gone, or the user meanwhile has a confirmed device (a second, parallel enrolment)."""
    if not TOTP_CODE.match(token):
        return None
    with transaction.atomic():
        StaffUser.objects.select_for_update().filter(pk=user.pk).first()  # one enrolment at a time per user
        if TOTPDevice.objects.filter(user=user, confirmed=True).exists():
            return None
        device = (
            TOTPDevice.objects.select_for_update().filter(pk=device_id, user=user, confirmed=False).first()
        )
        if device is None or not device.verify_token(token):
            return None
        device.confirmed = True
        device.save(update_fields=["confirmed"])
        TOTPDevice.objects.filter(user=user).exclude(pk=device.pk).delete()
        codes = issue_recovery_codes(user)
    return Enrolment(device, codes)


def verify_totp(user: StaffUser, token: str) -> TOTPDevice | None:
    """The user's confirmed device when ``token`` is its current code (not used before), else ``None``."""
    device = confirmed_totp(user)
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
        scale=5, border=3, dark="#000000", light="#ffffff", svgclass="ops-qr", title="TOTP QR kod"
    )
    return mark_safe(svg)  # noqa: S308 - generated by segno from our own URI; no user markup


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


def issue_recovery_codes(user: StaffUser) -> list[str]:
    """Replace the user's recovery codes with ten new ones; the plain codes are returned once, only their
    keyed hashes are stored."""
    codes = [
        "".join(secrets.choice(RECOVERY_ALPHABET) for _ in range(RECOVERY_LENGTH))
        for _ in range(RECOVERY_CODE_COUNT)
    ]
    with transaction.atomic():
        StaticDevice.objects.filter(user=user).delete()
        device = StaticDevice.objects.create(user=user, name=RECOVERY_DEVICE_NAME, confirmed=True)
        StaticToken.objects.bulk_create(
            StaticToken(device=device, token=recovery_hash(code)) for code in codes
        )
    half = RECOVERY_LENGTH // 2
    return [f"{code[:half]}-{code[half:]}" for code in codes]


@dataclass(frozen=True)
class RecoveryUse:
    device: StaticDevice
    remaining: int


def use_recovery_code(user: StaffUser, code: str) -> RecoveryUse | None:
    """Consume one recovery code of ``user`` (the row is deleted: a code works once); ``None`` if no unused
    code matches."""
    normalized = normalize_recovery_code(code)
    if len(normalized) != RECOVERY_LENGTH or not set(normalized) <= set(RECOVERY_ALPHABET):
        return None
    with transaction.atomic():
        token = (
            StaticToken.objects.select_for_update()
            .select_related("device")
            .filter(device__user=user, device__confirmed=True, token__in=_candidate_hashes(normalized))
            .first()
        )
        if token is None:
            return None
        device = token.device
        token.delete()
        device.set_last_used_timestamp(commit=True)
        remaining = StaticToken.objects.filter(device__user=user, device__confirmed=True).count()
    return RecoveryUse(device, remaining)


def recovery_codes_left(user: StaffUser) -> int:
    return int(StaticToken.objects.filter(device__user=user, device__confirmed=True).count())


# --- Reset -----------------------------------------------------------------------------------------------


def reset_devices(user: StaffUser) -> dict[str, int]:
    """Delete every TOTP device and recovery code of ``user``: the next login enrols again, and a session
    verified by a deleted device stops being verified at once (``OTPMiddleware`` finds no device)."""
    with transaction.atomic():
        totp, _ = TOTPDevice.objects.filter(user=user).delete()
        codes = StaticToken.objects.filter(device__user=user).count()
        StaticDevice.objects.filter(user=user).delete()
    return {"totp_devices": totp, "recovery_codes": codes}
