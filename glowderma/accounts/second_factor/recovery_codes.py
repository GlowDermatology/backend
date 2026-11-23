"""One-time recovery codes: issued ten at a time, stored only as keyed hashes, consumed once (package doc)."""

from __future__ import annotations

import base64
import re
import secrets
from dataclasses import dataclass

from django.conf import settings
from django.db import transaction
from django.utils.crypto import salted_hmac
from django_otp.plugins.otp_static.models import StaticDevice, StaticToken

from glowderma.accounts.models import StaffUser
from glowderma.accounts.second_factor.scopes import DeviceScope

RECOVERY_CODE_COUNT = 10
RECOVERY_ALPHABET = "abcdefghijklmnopqrstuvwxyz234567"
RECOVERY_LENGTH = 10
RECOVERY_SALT = "glowderma.accounts.console.recovery-code"


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


__all__ = [
    "RECOVERY_ALPHABET",
    "RECOVERY_CODE_COUNT",
    "RECOVERY_LENGTH",
    "RECOVERY_SALT",
    "RecoveryUse",
    "issue_recovery_codes",
    "normalize_recovery_code",
    "recovery_codes_left",
    "recovery_hash",
    "use_recovery_code",
]
