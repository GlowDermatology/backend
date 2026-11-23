"""``PUT me/device/`` (customer contract §2.2.6): registering a device and its push token. A token another
customer holds moves to the caller, with that customer locked first (one lock order, no deadlock).
"""

from __future__ import annotations

import hashlib
import logging

from django.db import transaction
from django.db.models import Q
from rest_framework.exceptions import ValidationError

from glowderma.accounts.models import AuthSession, Device
from glowderma.accounts.services.customer import auth_sessions, locks
from glowderma.core import enums, timeutils
from glowderma.core.api.exceptions import AuthFailed, RequestInProgress, field_error
from glowderma.core.pii import pii_ref

logger = logging.getLogger("glowderma.accounts.services")


# Attempts of ``register_device`` whose locked read finds a token holder the unlocked read missed. Under the
# per-token lock only a writer that bypasses ``register_device`` can add a holder (none does at request
# time), so a second attempt is never needed in practice; the bound keeps such a writer from looping a
# request forever.
REGISTER_DEVICE_ATTEMPTS = 3


class _TokenHoldersChanged(Exception):
    """The token moved to another customer's device between the unlocked and the locked read."""


def _lock_fcm_token(token: str) -> None:
    """``pg_advisory_xact_lock`` keyed on the SHA-256 of the FCM token (never the token itself, which would
    appear in the statement text): every ``register_device`` of one token waits for the previous one to end
    its transaction, so its holder read sees every committed holder."""
    digest = hashlib.sha256(f"gd.fcm:{token}".encode()).digest()
    with transaction.get_connection().cursor() as cursor:
        cursor.execute("SELECT pg_advisory_xact_lock(%s)", [int.from_bytes(digest[:8], "big", signed=True)])


def register_device(
    customer_id: int,
    fid: str,
    *,
    fcm_token: str | None,
    push_enabled: bool,
    model: str = "",
    os: str = "",
    locale: str = "",
    platform: str | None = None,
    app_version: str | None = None,
) -> Device:
    """Register or refresh the current device's facts and FCM token (``PUT me/device/``, customer contract
    §2.2.6).

    ``push_enabled`` with an empty token → field ``push_enabled`` ``push_token_missing``. A non-empty token
    held by another device row (the phone changed accounts) is taken from that row first (no token, no push,
    inactive if it was active), so ``device_active_fcm_token_unique`` never fires; a holder deactivated here
    also loses its open sessions (``replaced``, tombstones and those sessions' QR keys on commit, no farewell
    push: the token is this install's now), so that customer's app is signed out on every route instead of
    only on ``PUT me/device/``. ``fcm_invalidated_at`` is cleared when a new token arrives.

    Lock order (customer contract §2.7.1): a transaction-level advisory lock on the token first (keyed on its
    hash; only this function takes it, and nothing holds a customer lock before calling it), then the
    customer-level locks — this customer and every customer whose device holds the token, in ascending
    primary key order — then the device rows (this device and the token's holders in one query ordered by
    primary key), then the holders' sessions. Every other flow of a holder (logout, a signed-in write,
    ``DELETE me/``, a reset) takes that holder's customer lock before its sessions and devices, so they
    serialize with this one instead of deadlocking with it. The token lock serializes two customers
    registering the same new token (an account switch on the install still in flight): the second one reads
    the first one's committed device as a holder and takes the token from it, instead of both saving it and
    the second failing on ``device_active_fcm_token_unique``. Only this function gives a row a token at
    request time (the demo seeder aside), so under the token lock the holder set can only shrink between the
    unlocked holder read and the locked device read; should the locked read still find a holder whose
    customer is not locked, the attempt rolls back and starts again (never locking a customer after a
    device), and after ``REGISTER_DEVICE_ATTEMPTS`` the request answers 409 ``request_in_progress``
    (``Retry-After: 1``). Under the lock a customer who is no longer active (401 ``SESSION_EXPIRED``) or a
    device a logout or another account's sign-in on the install deactivated meanwhile (401
    ``SESSION_EXPIRED``; B-W1 retired ``SESSION_REPLACED``) is refused before anything is written, so no
    token lands on a deleted or signed-out device.
    """
    token = (fcm_token or "").strip()
    if push_enabled and not token:
        raise ValidationError(
            {"push_enabled": [field_error("push_token_missing", message_key="field.push_token_missing")]}
        )
    for _attempt in range(REGISTER_DEVICE_ATTEMPTS):
        try:
            return _register_device_attempt(
                customer_id,
                fid,
                token,
                push_enabled=push_enabled,
                model=model,
                os=os,
                locale=locale,
                platform=platform,
                app_version=app_version,
            )
        except _TokenHoldersChanged:
            continue
    logger.warning("register_device: the token holders kept changing customer_id=%s", customer_id)
    raise RequestInProgress()


@transaction.atomic
def _register_device_attempt(
    customer_id: int,
    fid: str,
    token: str,
    *,
    push_enabled: bool,
    model: str,
    os: str,
    locale: str,
    platform: str | None,
    app_version: str | None,
) -> Device:
    holder_customers: set[int] = set()
    if token:
        _lock_fcm_token(token)
        holder_customers = set(
            Device.objects.filter(fcm_token=token).values_list("customer_id", flat=True).distinct()
        )
    locked_customers = sorted(holder_customers | {customer_id})
    locked = {locked_id: locks.lock_customer(locked_id) for locked_id in locked_customers}
    stored = locked[customer_id]
    if stored.status != enums.CustomerStatus.ACTIVE:
        raise AuthFailed("SESSION_EXPIRED")
    instant = timeutils.now()
    wanted = Q(customer_id=customer_id, fid=fid)
    if token:
        wanted |= Q(fcm_token=token)
    rows = list(Device.objects.select_for_update().filter(wanted).order_by("pk"))
    if {row.customer_id for row in rows} - set(locked_customers):
        raise _TokenHoldersChanged
    device = next((row for row in rows if row.customer_id == customer_id and row.fid == fid), None)
    if device is None:
        raise Device.DoesNotExist(f"No device {pii_ref(fid)} for customer {customer_id}")
    if not device.is_active:
        raise AuthFailed("SESSION_EXPIRED")
    if token:
        holders = [row for row in rows if row.pk != device.pk]
        deactivated = [holder for holder in holders if holder.is_active]
        for holder in holders:
            holder.fcm_token = ""
            holder.push_enabled = False
            holder.is_active = False
            holder.save(update_fields=["fcm_token", "push_enabled", "is_active", "updated_at"])
        if deactivated:
            # The install now belongs to this customer: the holder's open session dies with its device, so
            # every route of that app answers SESSION_EXPIRED alike (not only PUT me/device).
            auth_sessions.revoke_open_sessions(
                AuthSession.objects.filter(device_id__in=[holder.pk for holder in deactivated]),
                enums.SessionRevokeReason.REPLACED,
                at=instant,
            )
    if token and token != device.fcm_token:
        device.fcm_invalidated_at = None
    device.fcm_token = token
    device.push_enabled = push_enabled
    device.model = model[:80]
    device.os = os[:40]
    device.locale = locale[:8]
    if platform:
        device.platform = platform
    if app_version:
        device.app_version = app_version
    device.last_active_at = instant
    device.save()
    return device


__all__ = ["REGISTER_DEVICE_ATTEMPTS", "register_device"]
