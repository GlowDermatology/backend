"""Revoking a customer's sign-in sessions one by one, all but the current one, or all of them, for the
customer and for staff (B-W1, customer contract §2.2.10, admin §2.6). Writes follow the customer lock order of
§2.7.1 (Customer → session → device)."""

from __future__ import annotations

import uuid
from typing import Any

from django.db import transaction

from glowderma.accounts.models import AuthSession, Customer
from glowderma.accounts.services.customer import auth_sessions, devices, locks
from glowderma.core import audit, enums, timeutils


class SessionNotFound(Exception):
    """No open session with that id belongs to the customer (the API answers 404)."""


def _locked_open_session(customer_id: int, session_id: str) -> AuthSession:
    try:
        uuid.UUID(str(session_id))
    except ValueError as exc:
        raise SessionNotFound from exc
    session = (
        AuthSession.objects.select_for_update(no_key=True)
        .filter(
            pk=session_id,
            audience="customer",
            customer_id=customer_id,
            revoked_at__isnull=True,
            absolute_expires_at__gt=timeutils.now(),
        )
        .first()
    )
    if session is None:
        raise SessionNotFound
    return session


@transaction.atomic
def revoke_open_customer_session(
    customer_id: int,
    session_id: str,
    *,
    actor: Any,
    actor_type: str,
    request: Any = None,
    current_sid: str | None = None,
) -> AuthSession:
    """Revoke one open session of the customer — the row menu "Chiqish" (customer: reason ``logout``) or the
    admin customer card (staff: reason ``admin_revoked``). Its tombstone and QR keys go on commit, its device
    is retired when no other session is left on it (no push). The customer's own current session may be
    revoked too (it is a logout). Audit ``customer.session_revoked {sid, current}``."""
    if actor_type == "customer" and current_sid:
        # the caller's own session must still be open under the lock (a concurrent revocation wins)
        locks.lock_signed_in_customer(customer_id, current_sid)
    else:
        locks.lock_customer(customer_id)
    session = _locked_open_session(customer_id, session_id)
    reason = (
        enums.SessionRevokeReason.LOGOUT
        if actor_type == "customer"
        else enums.SessionRevokeReason.ADMIN_REVOKED
    )
    auth_sessions.revoke_session_rows(AuthSession.objects.filter(pk=session.pk), reason)
    devices.retire_sessionless_devices(customer_id)
    audit.record(
        action="customer.session_revoked",
        entity_type="customer",
        entity_id=str(customer_id),
        actor=actor,
        actor_type=actor_type,
        request=request,
        after={"sid": str(session.pk), "current": str(session.pk) == str(current_sid or "")},
    )
    return session


@transaction.atomic
def revoke_other_customer_sessions(
    customer_id: int, current_sid: str, *, actor: Customer, request: Any = None
) -> int:
    """ "Boshqa qurilmalardan chiqish": every open session of the customer but ``current_sid`` → ``logout``;
    the devices left without one are retired. Re-checks under the customer lock that the caller's own
    session is still open (401 otherwise). Audit ``customer.sessions_revoked {count, scope: "others"}``.
    Returns how many sessions ended."""
    locks.lock_signed_in_customer(customer_id, current_sid)
    sessions, _farewell = auth_sessions.sign_out_sessions(
        customer_id, enums.SessionRevokeReason.LOGOUT, except_sid=current_sid
    )
    audit.record(
        action="customer.sessions_revoked",
        entity_type="customer",
        entity_id=str(customer_id),
        actor=actor,
        actor_type="customer",
        request=request,
        after={"count": len(sessions), "scope": enums.LogoutScope.OTHERS},
    )
    return len(sessions)


@transaction.atomic
def revoke_all_customer_sessions(customer_id: int, *, actor: Any, request: Any = None) -> int:
    """Staff sign every session of the customer out (the ops console's «Barcha seanslarni tugatish»: a lost
    phone, an account takeover): each open session → ``admin_revoked``, tombstones and QR keys on commit, the
    devices left without a session retired (no push), under the customer lock like
    :func:`revoke_open_customer_session`. Audit ``customer.sessions_revoked {count, scope: "all"}`` when at
    least one session ended; safe to repeat (0, nothing audited). Returns how many sessions ended."""
    locks.lock_customer(customer_id)
    sessions, _farewell = auth_sessions.sign_out_sessions(
        customer_id, enums.SessionRevokeReason.ADMIN_REVOKED, except_sid=None
    )
    if sessions:
        audit.record(
            action="customer.sessions_revoked",
            entity_type="customer",
            entity_id=str(customer_id),
            actor=actor,
            actor_type="staff",
            request=request,
            after={"count": len(sessions), "scope": enums.LogoutScope.ALL},
        )
    return len(sessions)


__all__ = [
    "SessionNotFound",
    "revoke_all_customer_sessions",
    "revoke_open_customer_session",
    "revoke_other_customer_sessions",
]
