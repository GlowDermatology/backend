"""Staff invite and password-reset links (admin contract §2.1, §2.16): single-use tokens stored as digests,
the sealed e-mail payload, and setting a password with a link.
"""

from __future__ import annotations

import secrets
from datetime import timedelta
from typing import Any

from django.conf import settings
from django.db import transaction
from rest_framework.exceptions import ErrorDetail, ValidationError

from glowderma.accounts import topics
from glowderma.accounts.console import state as console_state
from glowderma.accounts.errors import PasswordTokenInvalid
from glowderma.accounts.models import AuthSession, StaffPasswordToken, StaffUser
from glowderma.accounts.services.common import hashing, revocation
from glowderma.accounts.services.staff import auth_sessions, credentials, messages
from glowderma.core import audit, enums, outbox, secretbox, timeutils
from glowderma.core.api.exceptions import Conflict

# The sealed values of an ``email.staff_password_link`` payload open only for their own token id.
PASSWORD_LINK_AAD = b"email.staff_password_link"
PASSWORD_TOKEN_MAX_LENGTH = 128


def password_link_url(raw: str) -> str:
    """The admin panel's set-password page for the raw token (``ADMIN_PANEL_URL``)."""
    return f"{settings.ADMIN_PANEL_URL}/set-password?token={raw}"


def password_link_aad(token_id: int) -> bytes:
    return PASSWORD_LINK_AAD + b":" + str(token_id).encode()


def revoke_password_tokens(user: StaffUser, *, except_id: int | None = None) -> int:
    """Revoke ``user``'s open (unused, unrevoked) password links; returns how many."""
    rows = StaffPasswordToken.objects.filter(staff=user, used_at__isnull=True, revoked_at__isnull=True)
    if except_id is not None:
        rows = rows.exclude(pk=except_id)
    return rows.update(revoked_at=timeutils.now())


@transaction.atomic
def issue_password_token(
    user: StaffUser, purpose: str, *, actor: StaffUser | None
) -> tuple[StaffPasswordToken, str]:
    """A new single-use password link of ``user`` (``invite`` or ``reset``), valid
    ``STAFF_INVITE_TTL_HOURS``; earlier open links are revoked. Returns the row and the raw token (32 random
    bytes, URL-safe), which exists nowhere else: only its SHA-256 is stored."""
    if purpose not in enums.StaffPasswordPurpose.values:
        raise ValueError(f"Unknown password link purpose {purpose!r}")
    revoke_password_tokens(user)
    raw = secrets.token_urlsafe(32)
    row = StaffPasswordToken.objects.create(
        staff=user,
        purpose=purpose,
        token_hash=hashing.hash_secret(raw),
        created_by=actor,
        expires_at=timeutils.now() + timedelta(hours=int(settings.STAFF_INVITE_TTL_HOURS)),
    )
    return row, raw


@transaction.atomic
def issue_password_link(
    user: StaffUser,
    purpose: str,
    *,
    actor: StaffUser | None,
    request: Any = None,
    via: str,
    reason: str = "",
) -> StaffPasswordToken:
    """Issue a password link (:func:`issue_password_token`) and e-mail it to ``user`` in their language
    through the outbox topic ``email.staff_password_link``: the address, the link and the name travel sealed
    (``core.secretbox``, bound to the token id) and are blanked once the message is final. Audited
    ``staff.password_link_sent`` (``purpose``, ``via``: create, reset, resend or self, ``expires_at``,
    ``link_id`` — never the link). 409 ``conflict`` when e-mail links are not available."""
    if not credentials.staff_email_invite_available():
        raise Conflict(messages.STAFF_LINK_UNAVAILABLE)
    row, raw = issue_password_token(user, purpose, actor=actor)
    aad = password_link_aad(row.pk)
    outbox.enqueue(
        topics.STAFF_PASSWORD_LINK,
        {
            "link_id": row.pk,
            "to": secretbox.seal(user.email.encode("utf-8"), aad=aad),
            "link": secretbox.seal(password_link_url(raw).encode("utf-8"), aad=aad),
            "name": secretbox.seal(user.full_name.encode("utf-8"), aad=aad),
            "lang": user.language,
            "purpose": purpose,
        },
        dedupe_key=topics.staff_password_link_key(row.pk),
    )
    audit.record(
        action="staff.password_link_sent",
        entity_type="staff",
        entity_id=str(user.pk),
        actor=actor,
        request=request,
        after={"purpose": purpose, "via": via, "expires_at": row.expires_at, "link_id": row.pk},
        reason=reason,
    )
    return row


@transaction.atomic
def send_password_link(
    user: StaffUser, *, actor: StaffUser | None, request: Any = None, refuse_self: bool = True
) -> StaffPasswordToken:
    """A manager (re)sends ``user`` a password link (``POST staff/{id}/send-password-link/``); the password
    itself is not changed and earlier links are revoked. ``invite`` for an account that has never signed in
    nor set a password, else ``reset``. 409 ``conflict``: the caller's own account (``refuse_self``), an
    inactive account, e-mail links not available."""
    if refuse_self and actor is not None and actor.pk == user.pk:
        raise Conflict(messages.STAFF_LINK_SELF)
    stored = StaffUser.objects.select_for_update().get(pk=user.pk)
    if not stored.is_active:
        raise Conflict(messages.STAFF_LINK_INACTIVE)
    fresh = stored.password_changed_at is None and not AuthSession.objects.filter(staff=stored).exists()
    purpose = enums.StaffPasswordPurpose.INVITE if fresh else enums.StaffPasswordPurpose.RESET
    return issue_password_link(stored, purpose, actor=actor, request=request, via="resend")


@transaction.atomic
def request_own_password_link(user: StaffUser, *, request: Any = None) -> StaffPasswordToken:
    """``POST auth/password-link/``: a staff member who must change their password but cannot type the current
    one (signed in with a social identity, an e-mail-link account) gets a reset link at their own address.
    409 ``conflict`` when the account need not change its password or e-mail links are not available."""
    stored = StaffUser.objects.select_for_update().get(pk=user.pk)
    if not stored.must_change_password:
        raise Conflict(messages.STAFF_LINK_NOT_PENDING)
    return issue_password_link(
        stored, enums.StaffPasswordPurpose.RESET, actor=stored, request=request, via="self"
    )


def _password_token(raw: object) -> StaffPasswordToken | None:
    if not isinstance(raw, str) or not raw or len(raw) > PASSWORD_TOKEN_MAX_LENGTH:
        return None
    return (
        StaffPasswordToken.objects.select_related("staff").filter(token_hash=hashing.hash_secret(raw)).first()
    )


def _token_usable(row: StaffPasswordToken | None) -> bool:
    return (
        row is not None
        and row.used_at is None
        and row.revoked_at is None
        and row.expires_at > timeutils.now()
        and row.staff.is_active
    )


def password_token(raw: object) -> StaffPasswordToken:
    """The open password link of ``raw`` with its staff member (``auth/password-token/check/``), else 400
    ``token_invalid`` — one answer for unknown, used, revoked, expired and an inactive account."""
    row = _password_token(raw)
    if row is None or not _token_usable(row):
        raise PasswordTokenInvalid
    return row


@transaction.atomic
def set_password_with_token(raw: object, new_password: str, *, request: Any = None) -> StaffUser:
    """``auth/password-token/set/``: under the staff row lock (then the link's), the link must still be open
    and the account active (else 400 ``token_invalid``); the password becomes ``new_password`` (validated by
    the caller against ``AUTH_PASSWORD_VALIDATORS`` for this user; the current one again → 400
    ``password_unchanged``), ``must_change_password`` is cleared and ``password_changed_at`` stamped, the link
    is used and every other link revoked, every API session is revoked (``password_changed``) and the ops
    console sessions end; a running login lockout ends once it commits. No session is opened (the person
    signs in next). Audited ``staff.password_set`` (actor: the person) plus ``auth.session_revoked``."""
    found = _password_token(raw)
    if found is None:
        raise PasswordTokenInvalid
    user = StaffUser.objects.select_for_update().get(pk=found.staff_id)
    row = StaffPasswordToken.objects.select_for_update().get(pk=found.pk)
    row.staff = user
    if not _token_usable(row):
        raise PasswordTokenInvalid
    if user.has_usable_password() and user.check_password(new_password):
        raise ValidationError(
            {"new_password": [ErrorDetail(messages.PASSWORD_UNCHANGED, code="password_unchanged")]}
        )
    instant = timeutils.now()
    user.set_password(new_password)
    user.must_change_password = False
    user.password_changed_at = instant
    user.save(update_fields=["password", "must_change_password", "password_changed_at"])
    row.used_at = instant
    row.save(update_fields=["used_at"])
    revoke_password_tokens(user, except_id=row.pk)
    sessions = auth_sessions.revoke_user_sessions_locked(user, "password_changed")
    console_sessions = console_state.end_console_sessions(user)
    credentials.unlock_on_commit(user.email)
    details: dict[str, Any] = {"purpose": row.purpose, "link_id": row.pk, "revoked_count": len(sessions)}
    if console_sessions:
        details["console_sessions"] = console_sessions
    audit.record(
        action="staff.password_set",
        entity_type="staff",
        entity_id=str(user.pk),
        actor=user,
        request=request,
        after=details,
    )
    revocation.audit_revoked_sessions(sessions, actor=user, request=request, reason="")
    return user


def record_own_password_change(user: StaffUser) -> bool:
    """After ``auth/change-password/`` set a new password on the locked ``user`` (not yet saved): clear
    ``must_change_password``, stamp ``password_changed_at``, save, revoke the open password links. Returns
    whether the change was forced (the flag was set)."""
    forced = bool(user.must_change_password)
    user.must_change_password = False
    user.password_changed_at = timeutils.now()
    user.save(update_fields=["password", "must_change_password", "password_changed_at"])
    revoke_password_tokens(user)
    return forced


__all__ = [
    "PASSWORD_LINK_AAD",
    "PASSWORD_TOKEN_MAX_LENGTH",
    "password_link_url",
    "password_link_aad",
    "revoke_password_tokens",
    "issue_password_token",
    "issue_password_link",
    "send_password_link",
    "request_own_password_link",
    "password_token",
    "set_password_with_token",
    "record_own_password_change",
]
