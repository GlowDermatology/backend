"""Locked reads of customer rows: the repository every customer write goes through (customer contract §2.7.1).

Every customer lock is ``FOR NO KEY UPDATE``, never a plain ``FOR UPDATE``. The lock order is always
Customer → Address / identity → AuthSession → Device → SmsCode.
"""

from __future__ import annotations

import enum
import uuid

from rest_framework.exceptions import NotFound

from glowderma.accounts.models import Address, AuthSession, Customer
from glowderma.accounts.services.common import revocation
from glowderma.accounts.services.otp import targets
from glowderma.core import enums, timeutils
from glowderma.core.api.exceptions import AuthFailed


def lock_customer(customer_id: int) -> Customer:
    """The customer-level lock (customer contract §2.7.1): ``FOR NO KEY UPDATE``, never a plain ``FOR
    UPDATE``, so foreign-key inserts that take ``FOR KEY SHARE`` (a capture's ``Purchase``) never wait for
    it."""
    return Customer.objects.select_for_update(no_key=True).get(pk=customer_id)


def lock_customer_by_phone(identifier: str | targets.OtpTarget) -> Customer | None:
    """The customer-level lock of the row holding the phone — or, given an address, the e-mail — (any status),
    or ``None``."""
    return (
        Customer.objects.select_for_update(no_key=True)
        .filter(**targets.otp_target(identifier).lookup)
        .first()
    )


class NoSession(enum.Enum):
    """The explicit "no session" of a signed-in write service called outside a customer request (staff,
    tasks): the session re-check is skipped only when a caller says so, never because ``sid`` was omitted."""

    NO_SESSION = "no_session"


NO_SESSION = NoSession.NO_SESSION


def lock_signed_in_customer(customer_id: int, sid: uuid.UUID | str | NoSession) -> Customer:
    """The customer-level lock of a signed-in write (customer contract §2.2, §6.17), re-checked under it.

    A request authenticated before a concurrent ``DELETE me/``, a reset or a session revocation committed must
    not write to the account afterwards: the customer must still be ``active`` and the request's session
    (``sid``, required: :data:`NO_SESSION` only for a caller without a customer session) still open and
    unexpired, else 401 (``password_changed`` for that revocation, ``SESSION_EXPIRED`` otherwise). The
    session row is locked (``FOR NO KEY UPDATE``) after the customer, the order ``start_customer_session``
    uses, so a revocation either committed before (refused here) or waits for this write.
    """
    stored = lock_customer(customer_id)
    if stored.status != enums.CustomerStatus.ACTIVE:
        raise AuthFailed("SESSION_EXPIRED")
    if not isinstance(sid, NoSession):
        session = (
            AuthSession.objects.select_for_update(no_key=True)
            .filter(pk=sid, audience="customer", customer_id=customer_id)
            .first()
        )
        if session is None:
            raise AuthFailed("SESSION_EXPIRED")
        if session.revoked_at is not None:
            revocation.raise_revoked_session(session.revoke_reason)
        if session.absolute_expires_at <= timeutils.now():
            raise AuthFailed("SESSION_EXPIRED")
    return stored


def lock_address(address_id: int, customer_id: int) -> Address:
    address = Address.objects.select_for_update().filter(pk=address_id, customer_id=customer_id).first()
    if address is None:
        raise NotFound
    return address


__all__ = [
    "lock_customer",
    "lock_customer_by_phone",
    "NoSession",
    "NO_SESSION",
    "lock_signed_in_customer",
    "lock_address",
]
