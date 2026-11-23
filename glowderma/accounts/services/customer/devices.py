"""Customer devices (customer contract §2.2.6): the device key check, retiring devices left without an open
session, clearing keys and dropping a push token FCM no longer accepts.
"""

from __future__ import annotations

import hmac
from datetime import datetime
from typing import Any

from django.db.models import Exists, OuterRef, Q

from glowderma.accounts.models import AuthSession, Device
from glowderma.accounts.services.common import hashing
from glowderma.core import timeutils

DEVICE_KEY_HEADER_LENGTH = 43  # secrets.token_urlsafe(32)


def _retire_devices(queryset: Any, *, at: datetime) -> list[str]:
    """Deactivate the active devices of ``queryset`` (no push, no token; the key hash stays) and return the
    FCM tokens they held (the farewell candidates of a security notice; an active token is unique, so no
    other active row holds one of them)."""
    rows = list(queryset.filter(is_active=True).select_for_update().order_by("pk"))
    if not rows:
        return []
    Device.objects.filter(pk__in=[row.pk for row in rows]).update(
        is_active=False, push_enabled=False, fcm_token="", updated_at=at
    )
    return list(dict.fromkeys(row.fcm_token for row in rows if row.fcm_token))


def _open_session_exists() -> Exists:
    return Exists(
        AuthSession.objects.filter(
            device_id=OuterRef("pk"),
            audience="customer",
            revoked_at__isnull=True,
            absolute_expires_at__gt=timeutils.now(),
        )
    )


def retire_sessionless_devices(
    customer_id: int, *, keep_device_id: int | None = None, at: datetime | None = None
) -> list[str]:
    """Deactivate every active device of the customer left without an open session (B-W1, customer contract
    §1.9): a signed-out phone stops receiving pushes. Returns the farewell tokens (:func:`_retire_devices`).
    Runs under the caller's customer-level lock."""
    queryset = Device.objects.filter(customer_id=customer_id).exclude(_open_session_exists())
    if keep_device_id is not None:
        queryset = queryset.exclude(pk=keep_device_id)
    return _retire_devices(queryset, at=at or timeutils.now())


def customers_with_sessionless_devices() -> list[int]:
    """Customers holding an active device without an open, unexpired session (``accounts.cleanup_auth``)."""
    return sorted(
        set(
            Device.objects.filter(is_active=True)
            .exclude(_open_session_exists())
            .values_list("customer_id", flat=True)
        )
    )


def device_is_known(customer_id: int, fid: str, raw_device_key: str | None) -> bool:
    """Whether ``Device(customer, fid)`` holds the key (customer contract §1.9: the fid alone never does).

    A missing, malformed or wrong key is simply "unknown" (never an error); compared with ``compare_digest``.
    """
    if not raw_device_key or len(raw_device_key) != DEVICE_KEY_HEADER_LENGTH:
        return False
    stored = (
        Device.objects.filter(customer_id=customer_id, fid=fid).values_list("key_hash", flat=True).first()
    )
    if not stored:
        return False
    return hmac.compare_digest(stored, hashing.hash_secret(raw_device_key))


def invalidate_fcm_token(device_id: int, *, token: str | None = None) -> bool:
    """FCM says the token is dead (customer contract §2.14.2): ``fcm_token=""``, ``push_enabled=False``,
    ``fcm_invalidated_at=now``. With ``token``, only while the device still holds that token (a newer one sent
    through ``PUT me/device/`` meanwhile is kept). Returns whether the row changed."""
    queryset = Device.objects.filter(pk=device_id)
    if token is not None:
        queryset = queryset.filter(fcm_token=token)
    instant = timeutils.now()
    return bool(
        queryset.exclude(Q(fcm_token="") & Q(push_enabled=False)).update(
            fcm_token="", push_enabled=False, fcm_invalidated_at=instant, updated_at=instant
        )
    )


def clear_device_keys(customer_id: int, *, except_fid: str | None = None) -> int:
    """Forget every device key of the customer (but ``except_fid``'s): those installs need the SMS code on
    their next password sign-in (reset, dormant reset; customer contract §2.1.5, §2.2.3)."""
    queryset = Device.objects.filter(customer_id=customer_id)
    if except_fid is not None:
        queryset = queryset.exclude(fid=except_fid)
    return queryset.exclude(key_hash="").update(key_hash="", updated_at=timeutils.now())


__all__ = [
    "DEVICE_KEY_HEADER_LENGTH",
    "retire_sessionless_devices",
    "customers_with_sessionless_devices",
    "device_is_known",
    "invalidate_fcm_token",
    "clear_device_keys",
]
