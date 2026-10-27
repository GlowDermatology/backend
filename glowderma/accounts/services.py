import enum
import hashlib
import hmac
import logging
import secrets
import uuid
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from django.conf import settings
from django.contrib.auth.hashers import check_password, make_password
from django.contrib.auth.password_validation import validate_password
from django.core.cache import cache
from django.core.exceptions import ValidationError as DjangoValidationError
from django.core.files.base import ContentFile
from django.db import IntegrityError, transaction
from django.db.models import Q, Sum
from rest_framework.exceptions import NotFound, PermissionDenied, ValidationError

from glowderma.accounts import tokens, topics
from glowderma.accounts.api.guard import LoginGuard, email_hash
from glowderma.accounts.models import (
    Address,
    AuthSession,
    Customer,
    CustomerSocialIdentity,
    Device,
    SmsCode,
    SocialIdentity,
    StaffUser,
)
from glowderma.core import alerts, audit, enums, outbox, qr_store, secretbox, timeutils
from glowderma.core.api.exceptions import (
    AuthFailed,
    Conflict,
    EmailNotRegistered,
    EmailRegistered,
    EmailServiceUnavailable,
    OtpDailyLimit,
    OtpExpired,
    OtpInvalid,
    OtpLocked,
    OtpNetworkLimit,
    OtpResendTooSoon,
    PasswordChanged,
    PhoneNotRegistered,
    PhoneRegistered,
    PhoneRequired,
    RequestInProgress,
    SmsUnavailable,
    VerificationInvalid,
    field_error,
)
from glowderma.core.models import IdempotencyRecord
from glowderma.core.pii import normalize_email as pii_normalize_email
from glowderma.core.pii import pii_ref
from glowderma.integrations.email import registry as email_registry
from glowderma.integrations.eskiz import registry as eskiz_registry
from glowderma.integrations.oauth.ports import SocialClaims

logger = logging.getLogger(__name__)

SOCIAL_SUBJECT_LINKED = "Bu ijtimoiy hisob allaqachon bog‘langan."
SOCIAL_PROVIDER_LINKED = "Bu provayder allaqachon bog‘langan."


@transaction.atomic
def start_staff_session(
    user: StaffUser,
    *,
    device_label: str,
    auth_method: str = AuthSession.AuthMethod.PASSWORD,
    failure_code: str = "invalid_credentials",
) -> tuple[AuthSession, str]:
    """Open a staff session; ``auth_method`` records how the sign-in was proven (``password`` for email +
    password, the provider for a social sign-in): only a password session may link an identity (§2.1).

    The session is created under the staff row lock that :func:`reset_staff_password` and
    :func:`deactivate_staff` also take, and the proof is checked again under it: the account must still be
    active and, for a password sign-in, its stored hash must still be the one ``user`` was authenticated
    with. A sign-in that raced a reset or a deactivation therefore never opens a session the revocation
    missed; it fails with ``failure_code`` (401) instead.
    """
    if auth_method not in AuthSession.AuthMethod.values:
        raise ValueError(f"Unknown auth method {auth_method!r}")
    locked = StaffUser.objects.select_for_update().filter(pk=user.pk).only("password", "is_active").first()
    if (
        locked is None
        or not locked.is_active
        or (auth_method == AuthSession.AuthMethod.PASSWORD and locked.password != user.password)
    ):
        raise AuthFailed(failure_code)
    refresh = secrets.token_urlsafe(32)
    session = AuthSession.objects.create(
        audience="staff",
        staff=user,
        device_label=device_label[:100],
        refresh_hash=hashlib.sha256(refresh.encode()).hexdigest(),
        absolute_expires_at=timeutils.now() + timedelta(seconds=settings.STAFF_REFRESH_TTL),
        auth_method=auth_method,
    )
    return session, refresh


def check_session(session: AuthSession) -> None:
    if session.revoked_at is not None:
        raise AuthFailed("SESSION_REPLACED" if session.revoke_reason == "replaced" else "SESSION_EXPIRED")
    if session.absolute_expires_at <= timeutils.now() or session.staff is None or not session.staff.is_active:
        raise AuthFailed("SESSION_EXPIRED")


@transaction.atomic
def refresh_staff_session(raw_refresh: str) -> AuthSession:
    session = (
        AuthSession.objects.select_for_update(of=("self",))
        .select_related("staff")
        .filter(refresh_hash=hashlib.sha256(raw_refresh.encode()).hexdigest(), audience="staff")
        .first()
    )
    if session is None:
        raise AuthFailed("not_authenticated")
    check_session(session)
    session.last_used_at = timeutils.now()
    session.save(update_fields=["last_used_at"])
    return session


def tombstone_ttl() -> int:
    """Seconds a ``revoked:<reason>`` tombstone outlives the last access token of either audience."""
    return max(int(settings.STAFF_ACCESS_TTL), int(settings.CUSTOMER_ACCESS_TTL)) + 60


def _tombstone_on_commit(sessions: Iterable[AuthSession]) -> None:
    """Write ``revoked:<reason>`` tombstones once the revocation committed.

    Best effort (``robust``): the revocation already committed, so a Redis error must not turn the request
    that made it into a 500 (the client would lose the tokens a sign-in just issued). An unwritten tombstone
    leaves at most the add-only ``auth:sid`` active entry, whose TTL is ≤ 60 s; the failure logs ERROR.
    """
    values = {f"auth:sid:{session.pk}": f"revoked:{session.revoke_reason}" for session in sessions}
    if not values:
        return

    def write() -> None:
        try:
            cache.set_many(values, timeout=tombstone_ttl())
        except Exception:
            logger.exception(
                "auth.tombstone_failed sids=%s", sorted(key.rsplit(":", 1)[-1] for key in values)
            )

    transaction.on_commit(write, robust=True)


@transaction.atomic
def revoke_session(session: AuthSession, reason: str) -> None:
    stored = AuthSession.objects.select_for_update().get(pk=session.pk)
    if stored.revoked_at is None:
        stored.revoked_at = timeutils.now()
        stored.revoke_reason = reason
        stored.save(update_fields=["revoked_at", "revoke_reason"])
    session.revoked_at = stored.revoked_at
    session.revoke_reason = stored.revoke_reason
    _tombstone_on_commit([stored])


@transaction.atomic
def revoke_user_sessions(user: StaffUser, reason: str, *, except_sid: uuid.UUID | None = None) -> int:
    return len(_revoke_user_sessions(user, reason, except_sid=except_sid))


def _revoke_user_sessions(
    user: StaffUser, reason: str, *, except_sid: uuid.UUID | None = None
) -> list[AuthSession]:
    queryset = AuthSession.objects.select_for_update().filter(staff=user, revoked_at__isnull=True)
    if except_sid is not None:
        queryset = queryset.exclude(pk=except_sid)
    sessions = list(queryset)
    instant = timeutils.now()
    for session in sessions:
        session.revoked_at = instant
        session.revoke_reason = reason
    if sessions:
        AuthSession.objects.filter(pk__in=[session.pk for session in sessions]).update(
            revoked_at=instant, revoke_reason=reason
        )
    _tombstone_on_commit(sessions)
    return sessions


# --- staff management (admin contract §2.16; decisions GST-1..GST-6) ----------------------------------------

STAFF_EMAIL_TAKEN = "Bu email bilan xodim allaqachon bor (nofaol bo‘lsa, uni qayta faollashtiring)."
STAFF_LAST_MANAGER = "Kamida bitta faol menejer qolishi kerak. Avval boshqa xodimni menejer qiling."
STAFF_SELF_DEACTIVATE = "O‘z hisobingizni faolsizlantira olmaysiz."
STAFF_SELF_DEMOTE = "O‘zingizni menejerlikdan chiqara olmaysiz."
STAFF_SUPERUSER_MANAGER = "Superadmin doim menejer bo‘ladi: uni menejerlikdan chiqarib bo‘lmaydi."
STAFF_SELF_RESET = "O‘z parolingizni «Parolni o‘zgartirish» orqali almashtiring."
# Editable through ``PATCH staff/{id}/`` (the email is the sign-in identity and stays as created).
STAFF_EDITABLE_FIELDS = ("full_name", "is_manager")
# Login lockout scopes a password reset ends (``LoginView`` and the guarded Django admin login form).
STAFF_LOGIN_SCOPES = ("login", "django_admin_login")
# A temporary password: four groups of four characters without look-alikes (0/O, 1/l/I), ~92 bits.
TEMPORARY_PASSWORD_ALPHABET = "abcdefghjkmnpqrstuvwxyzABCDEFGHJKMNPQRSTUVWXYZ23456789"  # noqa: S105 - alphabet
TEMPORARY_PASSWORD_GROUPS = 4
TEMPORARY_PASSWORD_ATTEMPTS = 20


def generate_temporary_password(user: StaffUser) -> str:
    """A random ``xxxx-xxxx-xxxx-xxxx`` password that passes ``AUTH_PASSWORD_VALIDATORS`` for ``user``."""
    for _ in range(TEMPORARY_PASSWORD_ATTEMPTS):
        candidate = "-".join(
            "".join(secrets.choice(TEMPORARY_PASSWORD_ALPHABET) for _ in range(4))
            for _ in range(TEMPORARY_PASSWORD_GROUPS)
        )
        try:
            validate_password(candidate, user)
        except DjangoValidationError:  # pragma: no cover - ~92 random bits are never common or similar
            continue
        return candidate
    raise RuntimeError(
        "Could not generate a temporary password that passes the validators"
    )  # pragma: no cover


def _lock_active_managers() -> list[int]:
    """Lock the active managers (by id, one lock order for every caller) and return their ids.

    Every write that could leave no active manager (demotion, deactivation) takes these locks before the
    target row, so two managers demoting or deactivating each other at the same time serialize and the
    second one sees the first one's result (PostgreSQL re-checks the locked rows' conditions).
    """
    return list(
        StaffUser.objects.select_for_update()
        .filter(is_active=True, is_manager=True)
        .order_by("pk")
        .values_list("pk", flat=True)
    )


def _audit_revoked_sessions(
    sessions: Iterable[AuthSession], *, actor: Any, request: Any, reason: str
) -> None:
    for session in sessions:
        audit.record(
            action="auth.session_revoked",
            entity_type="auth_session",
            entity_id=str(session.pk),
            actor=actor,
            request=request,
            after={"sid": str(session.pk), "reason": session.revoke_reason},
            reason=reason,
        )


@transaction.atomic
def create_staff(
    *,
    email: str,
    full_name: str,
    is_manager: bool = False,
    password: str | None = None,
    is_superuser: bool = False,
    actor: StaffUser | None,
    request: Any = None,
    reason: str = "",
) -> tuple[StaffUser, str]:
    """Create an active staff account and return it with its password (admin contract §2.16 ``POST staff/``).

    ``password=None`` generates a temporary password (:func:`generate_temporary_password`) the caller shows
    once; ``manage.py createstaff`` passes the operator's validated password. The email is trimmed and
    lower-cased; an existing account with it (active or not, case-insensitive) is a ``unique`` field error.
    The first account of an install without an active manager becomes a manager, so staff management is
    never left without one (decision GST-1). A superuser is always a manager. Audited ``staff.created``.
    """
    email = email.strip().lower()
    if StaffUser.objects.filter(email__iexact=email).exists():
        raise ValidationError({"email": [field_error("unique", STAFF_EMAIL_TAKEN)]})
    manager = is_manager or is_superuser
    # Locked so two concurrent first accounts (or a concurrent demotion) see one another's result.
    if not manager and not _lock_active_managers():
        manager = True
    user = StaffUser(email=email, full_name=full_name, is_manager=manager)
    raw = password if password is not None else generate_temporary_password(user)
    try:
        with transaction.atomic():
            create = StaffUser.objects.create_superuser if is_superuser else StaffUser.objects.create_user
            user = create(email, raw, full_name=full_name, is_manager=manager)
    except IntegrityError as exc:  # lost a race with a concurrent create of the same email
        raise ValidationError({"email": [field_error("unique", STAFF_EMAIL_TAKEN)]}) from exc
    audit.record(
        action="staff.created",
        entity_type="staff",
        entity_id=str(user.pk),
        actor=actor,
        request=request,
        after={"email": user.email, "full_name": user.full_name, "is_manager": user.is_manager},
        reason=reason,
    )
    return user, raw


@transaction.atomic
def update_staff(
    user: StaffUser,
    changes: dict[str, Any],
    *,
    actor: StaffUser | None,
    request: Any = None,
    reason: str = "",
    refuse_self: bool = True,
) -> StaffUser:
    """Apply ``full_name`` / ``is_manager`` changes (admin contract §2.16 ``PATCH staff/{id}/``).

    Only the values that differ are written and audited (``staff.updated`` with ``before`` / ``after`` of the
    changed fields); no difference writes nothing. Removing the manager role is refused (409 ``conflict``)
    for the caller's own account (``refuse_self``; the operator commands pass ``False``), for a superuser
    (a superuser is always a manager, decision GST-1) and for the last active manager.
    """
    unknown = set(changes) - set(STAFF_EDITABLE_FIELDS)
    if unknown:
        raise ValueError(f"Not editable: {sorted(unknown)}")
    demoting = changes.get("is_manager") is False
    managers = _lock_active_managers() if demoting else []
    stored = StaffUser.objects.select_for_update().get(pk=user.pk)
    before: dict[str, Any] = {}
    after: dict[str, Any] = {}
    for name, value in changes.items():
        if getattr(stored, name) != value:
            before[name], after[name] = getattr(stored, name), value
    if after.get("is_manager") is False:
        if stored.is_superuser:
            raise Conflict(STAFF_SUPERUSER_MANAGER)
        if refuse_self and actor is not None and actor.pk == stored.pk:
            raise Conflict(STAFF_SELF_DEMOTE)
        if stored.is_active and managers == [stored.pk]:
            raise Conflict(STAFF_LAST_MANAGER)
    if not after:
        return stored
    for name, value in after.items():
        setattr(stored, name, value)
    stored.save(update_fields=list(after))
    audit.record(
        action="staff.updated",
        entity_type="staff",
        entity_id=str(stored.pk),
        actor=actor,
        request=request,
        before=before,
        after=after,
        reason=reason,
    )
    return stored


@transaction.atomic
def deactivate_staff(
    user: StaffUser,
    *,
    actor: StaffUser | None,
    request: Any = None,
    reason: str = "",
    refuse_self: bool = True,
) -> int:
    """Deactivate ``user`` and revoke every open session with reason ``deactivated``.

    Social identities stay linked (they sign in again after a reactivation; an inactive member's identity
    is refused like an unknown one). Refused (409 ``conflict``) for the caller's own account
    (``refuse_self``) and for the last active manager. Returns the number of revoked sessions; an already
    inactive user revokes nothing and writes nothing (0). Audited ``staff.deactivated`` plus one
    ``auth.session_revoked`` per session.
    """
    if refuse_self and actor is not None and actor.pk == user.pk:
        raise Conflict(STAFF_SELF_DEACTIVATE)
    managers = _lock_active_managers()
    stored = StaffUser.objects.select_for_update().get(pk=user.pk)
    if not stored.is_active:
        user.is_active = False
        return 0
    if stored.is_manager and managers == [stored.pk]:
        raise Conflict(STAFF_LAST_MANAGER)
    stored.is_active = False
    stored.save(update_fields=["is_active"])
    user.is_active = False
    sessions = _revoke_user_sessions(stored, "deactivated")
    audit.record(
        action="staff.deactivated",
        entity_type="staff",
        entity_id=str(stored.pk),
        actor=actor,
        request=request,
        before={"is_active": True},
        after={"is_active": False, "revoked_count": len(sessions)},
        reason=reason,
    )
    _audit_revoked_sessions(sessions, actor=actor, request=request, reason=reason)
    return len(sessions)


@transaction.atomic
def reactivate_staff(
    user: StaffUser, *, actor: StaffUser | None, request: Any = None, reason: str = ""
) -> bool:
    """Reactivate ``user``: it signs in again with its password or a linked identity; the sessions revoked at
    deactivation stay revoked and the manager role is kept. Returns False when the user was already active
    (nothing is written). Audited ``staff.reactivated``."""
    stored = StaffUser.objects.select_for_update().get(pk=user.pk)
    if stored.is_active:
        user.is_active = True
        return False
    stored.is_active = True
    stored.save(update_fields=["is_active"])
    user.is_active = True
    audit.record(
        action="staff.reactivated",
        entity_type="staff",
        entity_id=str(stored.pk),
        actor=actor,
        request=request,
        before={"is_active": False},
        after={"is_active": True},
        reason=reason,
    )
    return True


def _unlock_on_commit(email: str) -> None:
    """End the login lockouts of ``email`` once the surrounding transaction commits (a rolled-back reset
    leaves them). Best effort: a Redis error logs and never fails the committed reset."""
    hashed = email_hash(email)

    def unlock() -> None:
        guard = LoginGuard()
        for scope in STAFF_LOGIN_SCOPES:
            try:
                guard.unlock(scope, hashed)
            except Exception:
                logger.exception("auth.unlock_failed scope=%s", scope)

    transaction.on_commit(unlock, robust=True)


@transaction.atomic
def reset_staff_password(
    user: StaffUser,
    *,
    actor: StaffUser | None,
    request: Any = None,
    reason: str = "",
    password: str | None = None,
    refuse_self: bool = True,
) -> tuple[str, int]:
    """Replace ``user``'s password and revoke every open session (reason ``password_changed``).

    ``password=None`` generates a temporary password (returned once, never stored in clear or audited);
    ``manage.py resetstaffpassword --password-stdin`` passes the operator's validated one. The caller's own
    password is refused (``refuse_self``, 409 ``conflict``): staff change it with ``auth/change-password/``,
    which asks for the current one, so a stolen session cannot take the account over. Social identities stay
    linked. Works on an inactive account too (prepared before a reactivation). Returns ``(password,
    revoked_count)``; audited ``staff.password_reset`` plus one ``auth.session_revoked`` per session. Once the
    reset commits, a running login lockout of the account ends (both :data:`STAFF_LOGIN_SCOPES`), for the API
    and ``manage.py resetstaffpassword`` alike.
    """
    if refuse_self and actor is not None and actor.pk == user.pk:
        raise Conflict(STAFF_SELF_RESET)
    stored = StaffUser.objects.select_for_update().get(pk=user.pk)
    raw = password if password is not None else generate_temporary_password(stored)
    stored.set_password(raw)
    stored.save(update_fields=["password"])
    sessions = _revoke_user_sessions(stored, "password_changed")
    _unlock_on_commit(stored.email)
    audit.record(
        action="staff.password_reset",
        entity_type="staff",
        entity_id=str(stored.pk),
        actor=actor,
        request=request,
        after={"revoked_count": len(sessions), "generated": password is None},
        reason=reason,
    )
    _audit_revoked_sessions(sessions, actor=actor, request=request, reason=reason)
    return raw, len(sessions)


def _google_is_authoritative(claims: SocialClaims) -> bool:
    """Google vouches for the address: verified, and either a consumer @gmail.com account or a Workspace
    account whose ``hd`` is the email's own domain (and the configured ``GOOGLE_ALLOWED_HD`` when set)."""
    if not claims.email_verified or "@" not in claims.email:
        return False
    domain = claims.email.rsplit("@", 1)[1].lower()
    if domain == "gmail.com":
        return True
    if not claims.hd or claims.hd.lower() != domain:
        return False
    allowed = str(settings.GOOGLE_ALLOWED_HD).lower()
    return not allowed or claims.hd.lower() == allowed


def _autolink_candidate(provider: str, claims: SocialClaims) -> StaffUser | None:
    """The active staff user a first Google sign-in may be linked to (``STAFF_GOOGLE_AUTOLINK``), else None.

    One query: the user must have the (case-insensitive) email and no identity for the provider yet.
    """
    if provider != "google" or not settings.STAFF_GOOGLE_AUTOLINK or not _google_is_authoritative(claims):
        return None
    return (
        StaffUser.objects.filter(email__iexact=claims.email, is_active=True)
        .exclude(social_identities__provider=provider)
        .first()
    )


def _record_link(user: StaffUser, provider: str, claims: SocialClaims, *, auto: bool, request: Any) -> None:
    audit.record(
        action="auth.social_linked",
        entity_type="staff",
        entity_id=str(user.pk),
        actor=user,
        request=request,
        after={"provider": provider, "subject": claims.subject, "email": claims.email, "auto": auto},
    )


def social_login(
    provider: str, claims: SocialClaims, *, device_label: str, request: Any = None
) -> tuple[AuthSession, str]:
    """Sign a staff member in with verified provider claims (contract 2.1 ``POST auth/oauth/{provider}/``).

    Allow-list only: the identity ``(provider, subject)`` must already be linked to an active staff user,
    or (Google only, ``STAFF_GOOGLE_AUTOLINK``) an active staff user must carry the verified email so the
    identity can be linked on first use. Anything else is one uniform ``social_account_not_allowed``,
    whether or not the email belongs to anyone.
    """
    identity = (
        SocialIdentity.objects.select_related("staff")
        .filter(provider=provider, subject=claims.subject)
        .first()
    )
    user: StaffUser | None = None
    if identity is not None:
        user = identity.staff if identity.staff.is_active else None
    with transaction.atomic():
        if identity is None:
            user = _autolink_candidate(provider, claims)
            if user is not None:
                identity = SocialIdentity.objects.create(
                    staff=user, provider=provider, subject=claims.subject, email_at_link=claims.email
                )
                _record_link(user, provider, claims, auto=True, request=request)
        if user is not None and identity is not None:
            session, refresh = start_staff_session(
                user,
                device_label=device_label,
                auth_method=provider,
                failure_code="social_account_not_allowed",
            )
            identity.last_login_at = timeutils.now()
            identity.save(update_fields=["last_login_at"])
            audit.record(
                action="auth.login_succeeded",
                entity_type="staff",
                entity_id=str(user.pk),
                actor=user,
                request=request,
                after={"email": user.email, "method": provider},
            )
            return session, refresh
    # Outside the transaction so the audit row survives the 401 (the view's exception handler rolls back).
    audit.record(
        action="auth.login_failed",
        entity_type="staff",
        entity_id="",
        request=request,
        after={"email": claims.email, "method": provider},
    )
    raise AuthFailed("social_account_not_allowed")


def require_password_session(session_id: uuid.UUID | str) -> AuthSession:
    """The staff session ``session_id`` when its sign-in was an email + password one (admin contract §2.1:
    identities are linked only by a staff member signed in with the password), else 403
    ``permission_denied``. A session from before ``accounts.0007`` (no recorded method) is refused too."""
    session = AuthSession.objects.filter(pk=session_id, audience="staff").first()
    if session is None or session.auth_method != AuthSession.AuthMethod.PASSWORD:
        raise PermissionDenied
    return session


@transaction.atomic
def link_identity(
    user: StaffUser,
    provider: str,
    claims: SocialClaims,
    *,
    session_id: uuid.UUID | str | None = None,
    request: Any = None,
) -> SocialIdentity:
    """Link ``(provider, subject)`` to ``user`` (contract 2.1 ``POST auth/oauth/{provider}/link/``).

    Only from a password session (``session_id``; 403 ``permission_denied`` for a social one, so a social
    sign-in cannot attach a second identity). One identity per ``(staff, provider)`` and per ``(provider,
    subject)``; either duplicate is a 409.
    """
    if session_id is None:
        raise PermissionDenied
    require_password_session(session_id)
    if SocialIdentity.objects.filter(staff=user, provider=provider).exists():
        raise Conflict(SOCIAL_PROVIDER_LINKED)
    if SocialIdentity.objects.filter(provider=provider, subject=claims.subject).exists():
        raise Conflict(SOCIAL_SUBJECT_LINKED)
    try:
        with transaction.atomic():
            identity = SocialIdentity.objects.create(
                staff=user, provider=provider, subject=claims.subject, email_at_link=claims.email
            )
    except IntegrityError as exc:  # lost a race with a concurrent link
        raise Conflict(SOCIAL_SUBJECT_LINKED) from exc
    _record_link(user, provider, claims, auto=False, request=request)
    return identity


@transaction.atomic
def unlink_identity(user: StaffUser, provider: str, *, request: Any = None) -> None:
    """Remove the caller's identity for ``provider`` (contract 2.1 ``DELETE auth/oauth/{provider}/``)."""
    identity = SocialIdentity.objects.filter(staff=user, provider=provider).first()
    if identity is None:
        raise NotFound
    subject = identity.subject
    identity.delete()
    audit.record(
        action="auth.social_unlinked",
        entity_type="staff",
        entity_id=str(user.pk),
        actor=user,
        request=request,
        after={"provider": provider, "subject": subject},
    )


# --- customers (design 01 §1.1–§1.5: the model invariants B2 needs; the M2 API builds on them) ---


def _sync_address_text(customer_id: int) -> str:
    """``Customer.address_text`` = the default address, else the most recently updated one, else ``""``."""
    address = (
        Address.objects.filter(customer_id=customer_id).order_by("-is_default", "-updated_at", "-id").first()
    )
    text = address.text if address is not None else ""
    Customer.objects.filter(pk=customer_id).update(address_text=text, updated_at=timeutils.now())
    return text


@transaction.atomic
def add_address(
    customer_id: int,
    *,
    text: str,
    label: str = "",
    is_default: bool = False,
    region_code: str = "",
    city_code: str = "",
    comment: str = "",
    latitude: Decimal | None = None,
    longitude: Decimal | None = None,
) -> Address:
    """Create an address of the customer and recompute the denormalised ``address_text``.

    The first address of a customer always becomes the default; ``is_default=True`` replaces the previous one
    inside the same transaction (the partial unique constraint forbids two).
    """
    lock_customer(customer_id)
    if is_default or not Address.objects.filter(customer_id=customer_id).exists():
        Address.objects.filter(customer_id=customer_id, is_default=True).update(
            is_default=False, updated_at=timeutils.now()
        )
        is_default = True
    address = Address.objects.create(
        customer_id=customer_id,
        text=text,
        label=label,
        is_default=is_default,
        region_code=region_code,
        city_code=city_code,
        comment=comment,
        latitude=latitude,
        longitude=longitude,
    )
    _sync_address_text(customer_id)
    return address


_UNSET: Any = object()


@transaction.atomic
def update_address(
    address: Address,
    *,
    text: str = _UNSET,
    label: str = _UNSET,
    region_code: str = _UNSET,
    city_code: str = _UNSET,
    comment: str = _UNSET,
    latitude: Decimal | None = _UNSET,
    longitude: Decimal | None = _UNSET,
    is_default: bool = _UNSET,
) -> Address:
    """Edit an address (M2 ``PATCH /me/addresses/{id}``) and recompute ``Customer.address_text``.

    Only the keyword arguments given change. ``is_default=True`` clears the previous default inside the same
    transaction; ``is_default=False`` on the current default leaves the customer without one (``address_text``
    then falls back to the most recently updated address). The write path every edit must take so the design
    01 §1.3 invariant (saving an address recomputes the denormalised text) holds.
    """
    lock_customer(address.customer_id)
    changes = {
        name: value
        for name, value in (
            ("text", text),
            ("label", label),
            ("region_code", region_code),
            ("city_code", city_code),
            ("comment", comment),
            ("latitude", latitude),
            ("longitude", longitude),
            ("is_default", is_default),
        )
        if value is not _UNSET
    }
    if changes.get("is_default"):
        Address.objects.filter(customer_id=address.customer_id, is_default=True).exclude(
            pk=address.pk
        ).update(is_default=False, updated_at=timeutils.now())
    for name, value in changes.items():
        setattr(address, name, value)
    address.save(update_fields=[*changes])  # TimeStampedModel adds updated_at
    _sync_address_text(address.customer_id)
    return address


@transaction.atomic
def set_default_address(address: Address) -> Address:
    """Make ``address`` the customer's default, clearing the previous one in the same transaction."""
    lock_customer(address.customer_id)
    Address.objects.filter(customer_id=address.customer_id, is_default=True).exclude(pk=address.pk).update(
        is_default=False, updated_at=timeutils.now()
    )
    if not address.is_default:
        address.is_default = True
        address.save(update_fields=["is_default"])
    _sync_address_text(address.customer_id)
    return address


@transaction.atomic
def remove_address(address: Address) -> None:
    """Delete one address; the denormalised text falls back to the next best address (or ``""``)."""
    customer_id = address.customer_id
    lock_customer(customer_id)
    address.delete()
    _sync_address_text(customer_id)


def set_customer_password(customer: Customer, raw_password: str) -> None:
    customer.password = make_password(raw_password)
    customer.password_changed_at = timeutils.now()
    customer.save(update_fields=["password", "password_changed_at"])


def check_customer_password(customer: Customer, raw_password: str) -> bool:
    return bool(customer.password) and check_password(raw_password, customer.password)


@transaction.atomic
def delete_customer(customer: Customer | int) -> Customer:
    """Soft-delete a customer (decision A1, customer contract §2.2.3 step 5): scrub, revoke, deactivate; keep
    the row.

    Under the customer-level lock (``FOR NO KEY UPDATE``, so a concurrent capture's ``Purchase`` insert does
    not wait): ``status=deleted``; name, image, address text, password and e-mail address (hybrid identity)
    scrubbed; ``language`` reset to ``uz``;
    consent (``terms_version``, ``consent_at``, ``consent_ip``) and ``last_login_at`` cleared (the audit log
    keeps the consent history); addresses and social identities deleted (one ``apple.revoke`` outbox message
    per Apple identity holding a token); every device inactive with no push, token or key hash; every session
    revoked ``deactivated`` (tombstones and the QR keys' deletion on commit); the customer's stored
    Idempotency-Key responses deleted (they hold Me and Address payloads). ``LoyaltyAccount``,
    ``Purchase``, ``Order`` and ``Appointment`` rows keep pointing at it (``PROTECT``). Idempotent: an already
    deleted customer is returned unchanged.
    """
    customer_id = customer if isinstance(customer, int) else customer.pk
    stored = lock_customer(customer_id)
    if stored.status == enums.CustomerStatus.DELETED:
        return _refreshed(customer, stored)
    instant = timeutils.now()
    _delete_image_on_commit(stored)
    stored.status = enums.CustomerStatus.DELETED
    stored.deleted_at = instant
    stored.name = ""
    stored.address_text = ""
    stored.image = ""
    stored.password = ""
    stored.email = None  # hybrid identity: the address goes with the scrub (the phone stays, A1)
    stored.email_verified_at = None
    stored.language = enums.ContentLanguage.UZ
    stored.terms_version = ""
    stored.consent_at = None
    stored.consent_ip = None
    stored.last_login_at = None
    stored.save(
        update_fields=[
            "status",
            "deleted_at",
            "name",
            "address_text",
            "image",
            "password",
            "email",
            "email_verified_at",
            "language",
            "terms_version",
            "consent_at",
            "consent_ip",
            "last_login_at",
            "updated_at",
        ]
    )
    Address.objects.filter(customer_id=stored.pk).delete()
    for identity in CustomerSocialIdentity.objects.filter(customer_id=stored.pk):
        enqueue_apple_revoke(identity)
    CustomerSocialIdentity.objects.filter(customer_id=stored.pk).delete()
    Device.objects.filter(customer_id=stored.pk).update(
        is_active=False, push_enabled=False, fcm_token="", key_hash="", updated_at=instant
    )
    revoke_customer_sessions(stored.pk, enums.SessionRevokeReason.DEACTIVATED)
    # The stored responses of the customer's Idempotency-Key requests (Me and Address payloads: phone, name,
    # address text, coordinates) go with the scrub instead of outliving it by up to 24 h.
    IdempotencyRecord.objects.filter(audience="customer", actor_id=stored.pk).delete()
    return _refreshed(customer, stored)


def _delete_image_on_commit(customer: Customer) -> None:
    """Delete the customer's image file once the transaction that clears the column commits; a rollback
    keeps both the column and the file (a deleted file behind a kept name would break ``image_url``)."""
    if not customer.image:
        return
    storage, name = customer.image.storage, str(customer.image.name)
    transaction.on_commit(lambda: storage.delete(name), robust=True)


def _refreshed(customer: Customer | int, stored: Customer) -> Customer:
    if isinstance(customer, int):
        return stored
    customer.refresh_from_db()
    return customer


@transaction.atomic
def reactivate_customer(customer: Customer, *, name: str = "") -> Customer:
    """Re-registration with the same phone: ``active`` again, ``registered_at`` kept (first registration)."""
    stored = lock_customer(customer.pk)
    if stored.status == enums.CustomerStatus.ACTIVE:
        customer.refresh_from_db()
        return customer
    stored.status = enums.CustomerStatus.ACTIVE
    stored.deleted_at = None
    stored.reactivated_at = timeutils.now()
    if name:
        stored.name = name
    stored.save(update_fields=["status", "deleted_at", "reactivated_at", "name", "updated_at"])
    customer.refresh_from_db()
    return customer


# --- customer sessions, devices and keys (M2 P0: customer contract §1.9, §2.1.6, §2.2.3, §2.2.6) ---
# P1 appends the OTP and credential flows below this block.

DEVICE_KEY_HEADER_LENGTH = 43  # secrets.token_urlsafe(32)


@dataclass(frozen=True, slots=True)
class SessionStart:
    """What a successful customer sign-in produced (``start_customer_session``)."""

    session: AuthSession
    raw_refresh: str
    raw_device_key: str | None  # issued only with issue_device_key; returned once in AuthTokens.device_key
    farewell_tokens: list[str] = field(default_factory=list)  # FCM tokens of the devices step 2 deactivated


def hash_secret(raw: str) -> str:
    """SHA-256 hex of a refresh token or device key (the only form stored)."""
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def lock_customer(customer_id: int) -> Customer:
    """The customer-level lock (customer contract §2.7.1): ``FOR NO KEY UPDATE``, never a plain ``FOR
    UPDATE``, so foreign-key inserts that take ``FOR KEY SHARE`` (a capture's ``Purchase``) never wait for
    it."""
    return Customer.objects.select_for_update(no_key=True).get(pk=customer_id)


def customer_device_label(platform: str, model: str) -> str:
    """``"<platform> <model or 'app'>"``: the fid never appears in labels."""
    return f"{platform} {model or 'app'}"[:100]


def _revoke_qr_on_commit(customer_id: int) -> None:
    # robust: the data already committed, so a Redis error is logged, never turned into a 500 (the keys
    # expire after QR_TOKEN_TTL_SECONDS anyway).
    transaction.on_commit(lambda: qr_store.revoke_all(customer_id), robust=True)


@transaction.atomic
def start_customer_session(
    customer_id: int,
    fid: str,
    platform: str,
    app_version: str,
    *,
    issue_device_key: bool,
    auth_method: str = AuthSession.AuthMethod.PASSWORD,
) -> SessionStart:
    """Sign a customer in on one device (customer contract §1.9 steps 1–6, the single-device rule).

    Under the customer-level lock: (1) upsert ``Device(customer, fid)`` active; (2) every other device of the
    customer → inactive, no push, token cleared (captured first into ``farewell_tokens``); (3) every other
    open session → ``replaced`` (tombstones on commit) and the customer's QR keys deleted on commit; (4) one
    ``push.session_replaced`` per replaced session whose device had a token, unless that token is now held by
    another active device (the install changed accounts); (5) with ``issue_device_key`` a new device key whose
    SHA-256 replaces the stored hash; (6) a new session (sliding ``CUSTOMER_REFRESH_TTL_DAYS``) and
    ``Customer.last_login_at``.

    The customer's status is checked under the lock, not only by the caller: a sign-in racing ``DELETE me/``
    (or an admin delete) that commits first is refused with 401 ``SESSION_EXPIRED`` before anything is
    written, so a deleted account never regains a keyed device, an open session or ``last_login_at``
    (customer contract §2.2.3 step 5). The re-registration flow reactivates the customer before calling.
    """
    stored = lock_customer(customer_id)
    if stored.status != enums.CustomerStatus.ACTIVE:
        raise AuthFailed("SESSION_EXPIRED")
    instant = timeutils.now()
    device = Device.objects.select_for_update().filter(customer_id=customer_id, fid=fid).first()
    if device is None:
        device = Device.objects.create(
            customer_id=customer_id,
            fid=fid,
            platform=platform,
            app_version=app_version,
            is_active=True,
            last_active_at=instant,
        )
    else:
        device.is_active = True
        device.platform = platform
        device.app_version = app_version
        device.last_active_at = instant
        if (
            device.fcm_token
            and Device.objects.filter(is_active=True, fcm_token=device.fcm_token)
            .exclude(pk=device.pk)
            .exists()
        ):
            # Another account's active row took this install's token meanwhile; PUT me/device heals it.
            device.fcm_token = ""
            device.push_enabled = False
        device.save(
            update_fields=[
                "is_active",
                "platform",
                "app_version",
                "last_active_at",
                "fcm_token",
                "push_enabled",
                "updated_at",
            ]
        )
    others = list(Device.objects.select_for_update().filter(customer_id=customer_id).exclude(pk=device.pk))
    tokens_by_device = {other.pk: other.fcm_token for other in others if other.fcm_token}
    if others:
        Device.objects.filter(pk__in=[other.pk for other in others]).update(
            is_active=False, push_enabled=False, fcm_token="", updated_at=instant
        )
    # A token now held by another active row belongs to whoever uses that install next: never notified.
    held = set(
        Device.objects.filter(is_active=True, fcm_token__in=set(tokens_by_device.values())).values_list(
            "fcm_token", flat=True
        )
    )
    farewell_tokens = list(dict.fromkeys(token for token in tokens_by_device.values() if token not in held))
    replaced = _revoke_customer_sessions(customer_id, enums.SessionRevokeReason.REPLACED, at=instant)
    for old in replaced:
        token = tokens_by_device.get(old.device_id or 0, "")
        if not token or token in held:
            continue
        outbox.enqueue(
            topics.PUSH_SESSION_REPLACED,
            {"session_id": str(old.pk), "sid": str(old.pk), "fcm_token": token},
            dedupe_key=topics.session_replaced_key(old.pk),
        )
    raw_device_key: str | None = None
    if issue_device_key:
        raw_device_key = secrets.token_urlsafe(32)
        device.key_hash = hash_secret(raw_device_key)
        device.save(update_fields=["key_hash", "updated_at"])
    raw_refresh = secrets.token_urlsafe(32)
    session = AuthSession.objects.create(
        audience="customer",
        customer_id=customer_id,
        device=device,
        device_label=customer_device_label(platform, device.model),
        refresh_hash=hash_secret(raw_refresh),
        created_at=instant,
        last_used_at=instant,
        absolute_expires_at=instant + timedelta(days=int(settings.CUSTOMER_REFRESH_TTL_DAYS)),
        auth_method=auth_method,
    )
    Customer.objects.filter(pk=customer_id).update(last_login_at=instant, updated_at=instant)
    _revoke_qr_on_commit(customer_id)
    return SessionStart(session, raw_refresh, raw_device_key, farewell_tokens)


def _revoke_customer_sessions(
    customer_id: int, reason: str, *, except_sid: uuid.UUID | str | None = None, at: datetime | None = None
) -> list[AuthSession]:
    queryset = AuthSession.objects.filter(customer_id=customer_id)
    if except_sid is not None:
        queryset = queryset.exclude(pk=except_sid)
    return _revoke_open_customer_sessions(queryset, reason, at=at)


def _revoke_open_customer_sessions(
    queryset: Any, reason: str, *, at: datetime | None = None
) -> list[AuthSession]:
    """Lock and revoke the open customer sessions of ``queryset`` with ``reason`` (tombstones on commit)."""
    sessions = list(queryset.select_for_update().filter(audience="customer", revoked_at__isnull=True))
    instant = at or timeutils.now()
    for session in sessions:
        session.revoked_at = instant
        session.revoke_reason = reason
    if sessions:
        AuthSession.objects.filter(pk__in=[session.pk for session in sessions]).update(
            revoked_at=instant, revoke_reason=reason
        )
    _tombstone_on_commit(sessions)
    return sessions


@transaction.atomic
def revoke_customer_sessions(customer_id: int, reason: str, except_sid: uuid.UUID | str | None = None) -> int:
    """Revoke every open session of the customer (but ``except_sid``) with ``reason``.

    Tombstones ``revoked:<reason>`` are written on commit (never a plain delete) with the tombstone TTL, and
    the customer's outstanding QR keys are deleted on commit. Returns the number of sessions revoked.
    """
    if reason not in enums.SessionRevokeReason.values:
        raise ValueError(f"Unknown revoke reason {reason!r}")
    sessions = _revoke_customer_sessions(customer_id, reason, except_sid=except_sid)
    _revoke_qr_on_commit(customer_id)
    return len(sessions)


def move_history_horizon(customer_id: int, *, at: datetime) -> None:
    """Set ``Customer.history_from`` (customer contract §2.2.3): the customer's own history starts there."""
    Customer.objects.filter(pk=customer_id).update(history_from=at, updated_at=timeutils.now())


def apple_client_id(identity: CustomerSocialIdentity | None = None) -> str:
    """The ``client_id`` an Apple refresh token is revoked with (customer contract §2.1.8).

    Apple binds a refresh token to the client it was issued to: the ID token's ``aud`` bundle id, which the
    code exchange used as ``client_id``. It is kept on the identity (``provider_client_id``, and bound to the
    sealed token as its associated data, :func:`seal_apple_refresh`), so the revocation uses the right
    ``client_id`` when ``APPLE_BUNDLE_IDS`` lists several bundle ids. An identity from before
    ``accounts.0007`` falls back to the first configured bundle id (``""`` when none).
    """
    if identity is not None and identity.provider_client_id:
        return identity.provider_client_id
    bundle_ids = list(settings.APPLE_BUNDLE_IDS)
    return str(bundle_ids[0]) if bundle_ids else ""


def apple_token_aad(client_id: str) -> bytes:
    """Associated data of a sealed Apple refresh token: its ``client_id`` (a token cannot be replayed under
    another bundle id, and the revocation payload's ``client_id`` is authenticated by the seal)."""
    return f"apple.refresh_token:{client_id}".encode()


def seal_apple_refresh(refresh_token: str, *, client_id: str) -> str:
    return secretbox.seal(refresh_token.encode("utf-8"), aad=apple_token_aad(client_id))


def open_apple_refresh(sealed: str, *, client_id: str) -> str:
    return secretbox.open_(sealed, aad=apple_token_aad(client_id)).decode("utf-8")


def enqueue_apple_revoke(identity: CustomerSocialIdentity) -> bool:
    """Queue ``apple.revoke`` for an Apple identity holding a sealed refresh token (customer contract §2.1.8).

    Called in the transaction that unlinks the identity (``DELETE me/social/apple/``, the reset, the dormant
    reset, ``DELETE me/``). Returns whether a message was queued.
    """
    if identity.provider != enums.CustomerSocialProvider.APPLE or not identity.provider_refresh_token:
        return False
    outbox.enqueue(
        topics.APPLE_REVOKE,
        {
            "identity_id": identity.pk,
            **apple_grant_reference(identity.customer_id, identity.subject),
            "client_id": apple_client_id(identity),
            "sealed_token": identity.provider_refresh_token,
        },
        dedupe_key=topics.apple_revoke_key(identity.pk),
    )
    return True


def apple_grant_reference(customer_id: int | None, subject: str) -> dict[str, Any]:
    """The non-secret reference of an Apple grant in an ``apple.revoke`` payload: the customer, Apple's
    ``sub`` (the handler skips a grant whose subject is linked again, and staff need it to find a grant
    whose message went dead; blanked with the token after a final outcome) and ``subject_ref``
    (``pii_ref``, the only form the log lines carry)."""
    return {
        "customer_id": customer_id,
        "subject": subject,
        "subject_ref": pii_ref(subject) if subject else "",
    }


def apple_subject_linked(subject: str) -> bool:
    """Whether an Apple identity with ``subject`` is linked to any customer. Apple's revocation ends the
    user's whole authorization of the app, not only the one token, so a token whose subject is linked keeps
    the linked identity's grant alive and must never be revoked on its own."""
    return bool(subject) and (
        CustomerSocialIdentity.objects.filter(
            provider=enums.CustomerSocialProvider.APPLE, subject=subject
        ).exists()
    )


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
    also loses its open sessions (``replaced``, tombstones and its customer's QR keys on commit, no farewell
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
    device another sign-in or a logout deactivated meanwhile (401 ``SESSION_REPLACED`` when its newest
    session was replaced, else ``SESSION_EXPIRED``) is refused before anything is written, so no token lands
    on a deleted or replaced device.
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
    locked = {locked_id: lock_customer(locked_id) for locked_id in locked_customers}
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
        newest_reason = (
            AuthSession.objects.filter(device_id=device.pk)
            .order_by("-created_at")
            .values_list("revoke_reason", flat=True)
            .first()
        )
        replaced = newest_reason == enums.SessionRevokeReason.REPLACED
        raise AuthFailed("SESSION_REPLACED" if replaced else "SESSION_EXPIRED")
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
            # every route of that app answers SESSION_REPLACED alike (not only PUT me/device).
            _revoke_open_customer_sessions(
                AuthSession.objects.filter(device_id__in=[holder.pk for holder in deactivated]),
                enums.SessionRevokeReason.REPLACED,
                at=instant,
            )
            for holder_customer_id in {holder.customer_id for holder in deactivated}:
                _revoke_qr_on_commit(holder_customer_id)
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
    return hmac.compare_digest(stored, hash_secret(raw_device_key))


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


# --- P1: OTP, verification tokens, rotating refresh, logout, social nonce (customer contract §2.1) ---------
#
# Hybrid identity: one engine for codes sent by SMS (to a phone) and by e-mail (to an address). Every rule is
# keyed by the code's *target* (:class:`OtpTarget`): the per-target advisory lock, the lockout, the daily caps
# (codes and wrong codes), the resend window and the rows themselves; the fid and IP day counters are shared
# by both channels and each channel has its own global hourly breaker. Codes are stored as HMACs only.

# OtpPurpose (API) → SmsCode.Purpose (stored), customer contract §2.1.
OTP_PURPOSES: dict[str, str] = {
    enums.OtpPurpose.REGISTER: SmsCode.Purpose.SIGNUP,
    enums.OtpPurpose.LOGIN_NEW_DEVICE: SmsCode.Purpose.NEW_DEVICE,
    enums.OtpPurpose.RESET_PASSWORD: SmsCode.Purpose.RESET,
    enums.OtpPurpose.LINK_SOCIAL: SmsCode.Purpose.SOCIAL_LINK,
    enums.OtpPurpose.VERIFY_EMAIL: SmsCode.Purpose.VERIFY_EMAIL,
    enums.OtpPurpose.ATTACH_PHONE: SmsCode.Purpose.ATTACH_PHONE,
}
SMS_CHANNEL = "sms"
EMAIL_CHANNEL = "email"
# Purposes bound to one channel: attaching a phone is proven by SMS, verifying an e-mail by e-mail.
CHANNEL_ONLY_PURPOSES: dict[str, str] = {
    enums.OtpPurpose.ATTACH_PHONE: SMS_CHANNEL,
    enums.OtpPurpose.VERIFY_EMAIL: EMAIL_CHANNEL,
}
OTP_CODE_LENGTH = 6
OTP_PAYLOAD_KEY = "gd:otp:payload:{sms_code_id}"
SOCIAL_OTP_KEY = "gd:social:otp:{sms_code_id}"
SOCIAL_OTP_TTL_SECONDS = 15 * 60
SOCIAL_NONCE_KEY = "gd:social:nonce:{digest}"
SOCIAL_SEEN_KEY = "gd:social:seen:{digest}"
REFRESH_GRACE_KEY = "auth:refresh_grace:{digest}"
REFRESH_DEAD_KEY = "auth:refresh_dead:{digest}"


def normalize_email(value: str) -> str:
    """The stored form of an e-mail address (``core.pii.normalize_email``: NFC, lower case, punycode domain;
    one account per mailbox, C§2.1)."""
    return pii_normalize_email(value)


@dataclass(frozen=True, slots=True)
class OtpTarget:
    """Where a code goes: a phone (``sms``, E.164) or an e-mail address (``email``, normalised)."""

    channel: str
    value: str

    @property
    def is_email(self) -> bool:
        return self.channel == EMAIL_CHANNEL

    @property
    def lookup(self) -> dict[str, str]:
        """The ``SmsCode`` / ``Customer`` filter of this target."""
        return {"email": self.value} if self.is_email else {"phone": self.value}

    def codes(self) -> Any:
        return SmsCode.objects.filter(**self.lookup)

    @property
    def lock_key(self) -> str:
        # The phone key is unchanged ("otp:+998…"); an address can never collide with it.
        return f"otp:email:{self.value}" if self.is_email else f"otp:{self.value}"

    @property
    def log_ref(self) -> str:
        return f"{'email' if self.is_email else 'phone'}_ref={pii_ref(self.value)}"


def otp_target(identifier: str | OtpTarget) -> OtpTarget:
    """A target from a validated identifier: an address holds ``@``, an E.164 phone never does."""
    if isinstance(identifier, OtpTarget):
        return identifier
    if "@" in identifier:
        return OtpTarget(EMAIL_CHANNEL, normalize_email(identifier))
    return OtpTarget(SMS_CHANNEL, identifier)


def row_target(row: SmsCode) -> OtpTarget:
    return OtpTarget(EMAIL_CHANNEL, row.email) if row.email else OtpTarget(SMS_CHANNEL, row.phone)


def row_identifier(row: SmsCode) -> str:
    """The phone or the e-mail address a code row was sent to."""
    return row.email or row.phone


@dataclass(frozen=True, slots=True)
class CodeIssue:
    """``auth/send-code/`` 200 and the ``otp`` block of ``NextStep`` (customer contract §2.1.1, §2.1.4)."""

    sms_code_id: int
    phone: str  # "" for an e-mail code
    purpose: str
    expires_in: int
    resend_in: int
    attempts: int
    length: int = OTP_CODE_LENGTH
    email: str = ""  # "" for an SMS code

    @property
    def channel(self) -> str:
        return EMAIL_CHANNEL if self.email else SMS_CHANNEL


@dataclass(frozen=True, slots=True)
class Verification:
    """``auth/verify-code/`` 200: the raw token is returned once; only its SHA-256 is stored."""

    token: str
    expires_in: int
    purpose: str
    customer_exists: bool


def _seconds(delta: timedelta) -> int:
    return max(1, int(delta.total_seconds() + 0.999))


def _day_start(now: datetime) -> datetime:
    return timeutils.day_bounds(timeutils.local_date(now))[0]


def _to_midnight(now: datetime) -> int:
    return _seconds(timeutils.day_bounds(timeutils.local_date(now))[1] - now)


def _day_tag(now: datetime) -> str:
    return timeutils.local_date(now).strftime("%Y%m%d")


def _fid_counter_key(fid: str, now: datetime) -> str:
    return f"gd:otp:fid:{pii_ref(fid)}:{_day_tag(now)}"


def _ip_counter_key(ip: str, now: datetime) -> str:
    return f"gd:otp:ip:{pii_ref(ip)}:{_day_tag(now)}"


def _email_ip_counter_key(ip: str, now: datetime) -> str:
    return f"gd:otp:email_ip:{pii_ref(ip)}:{now.astimezone(timeutils.TASHKENT).strftime('%Y%m%d%H')}"


def _global_counter_key(now: datetime, channel: str = SMS_CHANNEL) -> str:
    prefix = "gd:otp:email_global" if channel == EMAIL_CHANNEL else "gd:otp:global"
    return f"{prefix}:{now.astimezone(timeutils.TASHKENT).strftime('%Y%m%d%H')}"


def _seconds_to_next_hour(now: datetime) -> int:
    local = now.astimezone(timeutils.TASHKENT)
    nxt = local.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
    return _seconds(nxt - local)


def _counter(key: str) -> int:
    try:
        return int(cache.get(key) or 0)
    except TypeError, ValueError:
        return 0


def _incr(key: str, timeout: int) -> int:
    """Atomic increment of a Redis day/hour counter (created with ``timeout`` on first use)."""
    cache.add(key, 0, timeout=timeout)
    try:
        return int(cache.incr(key))
    except ValueError:  # expired between add and incr
        cache.add(key, 1, timeout=timeout)
        return 1


def code_hmac(code: str) -> str:
    """``HMAC-SHA256(code, SMS_CODE_HMAC_KEY)`` hex: the only stored form of a code, SMS or e-mail (§6.4)."""
    return hmac.new(
        str(settings.SMS_CODE_HMAC_KEY).encode("utf-8"), code.encode("utf-8"), hashlib.sha256
    ).hexdigest()


def otp_payload_key(sms_code_id: int) -> str:
    return OTP_PAYLOAD_KEY.format(sms_code_id=sms_code_id)


def social_otp_key(sms_code_id: int) -> str:
    return SOCIAL_OTP_KEY.format(sms_code_id=sms_code_id)


def account_targets(target: OtpTarget) -> list[OtpTarget]:
    """The target and, when it belongs to an active account, the account's other identifier (its phone or
    e-mail): the lockout and the daily wrong-code cap are per account, so an account with both does not get
    two budgets. An unknown target is only itself (nothing is revealed)."""
    customer = Customer.objects.filter(**target.lookup, status=enums.CustomerStatus.ACTIVE).first()
    targets = [target]
    if customer is not None:
        if customer.phone and customer.phone != target.value:
            targets.append(OtpTarget(SMS_CHANNEL, customer.phone))
        if customer.email and customer.email != target.value:
            targets.append(OtpTarget(EMAIL_CHANNEL, customer.email))
    return targets


def _account_codes(target: OtpTarget) -> Any:
    condition = Q()
    for each in account_targets(target):
        condition |= Q(**each.lookup)
    return SmsCode.objects.filter(condition)


def _lock_target(target: OtpTarget) -> None:
    """``pg_advisory_xact_lock(hashtext('otp:' + phone))`` (``'otp:email:' + address`` for e-mail) for the
    target and its account's other identifier, in sorted order (no deadlock): serialises every OTP decision of
    one account until the surrounding transaction ends (customer contract §2.1.1)."""
    if not transaction.get_connection().in_atomic_block:
        raise RuntimeError("the per-target OTP lock needs a transaction")
    with transaction.get_connection().cursor() as cursor:
        for key in sorted({each.lock_key for each in account_targets(target)}):
            cursor.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", [key])


def active_customer_by_phone(identifier: str | OtpTarget) -> Customer | None:
    """The active customer of a phone — or, given an address (hybrid identity), of an e-mail."""
    target = otp_target(identifier)
    return Customer.objects.filter(**target.lookup, status=enums.CustomerStatus.ACTIVE).first()


def active_customer_by_email(email: str) -> Customer | None:
    return active_customer_by_phone(OtpTarget(EMAIL_CHANNEL, normalize_email(email)))


def _phone_block(target: OtpTarget, now: datetime) -> Exception | None:
    """The account-derived refusals (DB rows, they survive a cache flush): lockout, daily wrong-code cap —
    over the codes of the target and of its account's other identifier (:func:`account_targets`)."""
    lockout = timedelta(minutes=int(settings.CUSTOMER_OTP_LOCKOUT_MINUTES))
    exhausted = (
        _account_codes(target)
        .filter(exhausted_at__gt=now - lockout)
        .order_by("-exhausted_at")
        .values_list("exhausted_at", flat=True)
        .first()
    )
    if exhausted is not None:
        return OtpLocked(retry_after=_seconds(exhausted + lockout - now))
    if wrong_codes_today(target, now) >= int(settings.CUSTOMER_OTP_DAILY_WRONG_LIMIT):
        return OtpLocked(retry_after=_to_midnight(now))
    return None


def wrong_codes_today(identifier: str | OtpTarget, now: datetime) -> int:
    target = otp_target(identifier)
    return int(
        _account_codes(target)
        .filter(created_at__gte=_day_start(now))
        .aggregate(total=Sum("attempts"))["total"]
        or 0
    )


def _daily_limit(target: OtpTarget, now: datetime) -> Exception:
    key = "error.otp_daily_limit_email" if target.is_email else None
    return OtpDailyLimit(retry_after=_to_midnight(now), key=key)


def _cap_block(target: OtpTarget, *, fid: str, ip: str, now: datetime) -> Exception | None:
    """The day caps: codes per target (DB), per fid and per client IP (Redis counters)."""
    if target.codes().filter(created_at__gte=_day_start(now)).count() >= int(
        settings.CUSTOMER_OTP_DAILY_LIMIT
    ) or _counter(_fid_counter_key(fid, now)) >= int(settings.CUSTOMER_OTP_DEVICE_DAILY_LIMIT):
        return _daily_limit(target, now)
    if ip and _counter(_ip_counter_key(ip, now)) >= int(settings.CUSTOMER_OTP_IP_DAILY_LIMIT):
        return OtpNetworkLimit(retry_after=_to_midnight(now))
    # E-mail codes are cheap to request and share one hourly breaker: one IP may not exhaust it.
    if (
        target.is_email
        and ip
        and _counter(_email_ip_counter_key(ip, now)) >= int(settings.EMAIL_OTP_IP_HOURLY_LIMIT)
    ):
        return OtpNetworkLimit(retry_after=_seconds_to_next_hour(now))
    return None


def _budget_block(now: datetime, channel: str = SMS_CHANNEL) -> Exception | None:
    """The global hourly breaker of a channel: SMS → 503 ``sms_unavailable`` + ERROR ``otp.budget_tripped``
    and the alert of the same name; e-mail → 503 ``email_unavailable`` + ``otp.email_budget_tripped``, once
    per dedupe window. The alert insert belongs to the caller's transaction: ``auth/login/`` rolls its
    transaction back with the refusal (the ERROR line stays), ``auth/send-code/`` has none."""
    if channel == EMAIL_CHANNEL:
        limit = int(settings.EMAIL_GLOBAL_HOURLY_LIMIT)
        if _counter(_global_counter_key(now, channel)) >= limit:
            logger.error("otp.email_budget_tripped limit=%s", limit)
            alerts.alert("otp.email_budget_tripped", {"limit": limit})
            return EmailServiceUnavailable(retry_after=_seconds_to_next_hour(now))
        return None
    if _counter(_global_counter_key(now)) >= int(settings.SMS_GLOBAL_HOURLY_LIMIT):
        logger.error("otp.budget_tripped limit=%s", settings.SMS_GLOBAL_HOURLY_LIMIT)
        alerts.alert("otp.budget_tripped", {"limit": int(settings.SMS_GLOBAL_HOURLY_LIMIT)})
        return SmsUnavailable(retry_after=_seconds_to_next_hour(now))
    return None


def _count_issue(*, fid: str, ip: str, now: datetime, channel: str = SMS_CHANNEL) -> None:
    _incr(_global_counter_key(now, channel), 3600 + 60)
    if channel == EMAIL_CHANNEL and ip:
        _incr(_email_ip_counter_key(ip, now), 3600 + 60)
    _incr(_fid_counter_key(fid, now), 2 * 86400)
    if ip:
        _incr(_ip_counter_key(ip, now), 2 * 86400)


def _create_code(
    target: OtpTarget,
    stored_purpose: str,
    *,
    purpose: str,
    ip: str,
    fid: str,
    lang: str,
    platform: str,
    now: datetime,
) -> SmsCode:
    """Supersede the live code, store a new HMAC row, seal the plain code into Redis for the handler, count it
    and enqueue ``sms.send`` / ``email.send`` (no phone, no address, no text, no code in the payload)."""
    target.codes().filter(purpose=stored_purpose, consumed_at__isnull=True, expires_at__gt=now).update(
        expires_at=now
    )
    code = email_registry.new_code() if target.is_email else eskiz_registry.new_code()
    ttl = int(settings.CUSTOMER_OTP_TTL_SECONDS)
    row = SmsCode.objects.create(
        **target.lookup,
        purpose=stored_purpose,
        code_hmac=code_hmac(code),
        max_attempts=int(settings.CUSTOMER_OTP_MAX_ATTEMPTS),
        created_at=now,
        expires_at=now + timedelta(seconds=ttl),
        request_ip=ip or None,
    )
    cache.set(otp_payload_key(row.pk), secretbox.seal(code.encode("ascii"), aad=str(row.pk).encode()), ttl)
    _count_issue(fid=fid, ip=ip, now=now, channel=target.channel)
    if target.is_email:
        outbox.enqueue(
            topics.EMAIL_SEND,
            {"sms_code_id": row.pk, "lang": lang, "purpose": purpose},
            dedupe_key=topics.email_send_key(row.pk),
        )
        logger.info("otp.issued purpose=%s channel=email %s", purpose, target.log_ref)
    else:
        outbox.enqueue(
            topics.SMS_SEND,
            {"sms_code_id": row.pk, "lang": lang, "platform": platform},
            dedupe_key=topics.sms_send_key(row.pk),
        )
        logger.info("otp.issued purpose=%s phone_ref=%s", purpose, pii_ref(target.value))  # contract literal
    return row


def _issue(row: SmsCode, purpose: str, now: datetime) -> CodeIssue:
    resend_at = row.created_at + timedelta(seconds=int(settings.CUSTOMER_OTP_RESEND_SECONDS))
    return CodeIssue(
        sms_code_id=row.pk,
        phone=row.phone,
        email=row.email,
        purpose=purpose,
        expires_in=max(0, int((row.expires_at - now).total_seconds())),
        resend_in=max(0, int((resend_at - now).total_seconds() + 0.999)),
        attempts=row.max_attempts - row.attempts,
    )


def check_channel(purpose: str, target: OtpTarget) -> None:
    """A purpose bound to one channel refuses the other (a programming error: the serializers refuse it)."""
    channel = CHANNEL_ONLY_PURPOSES.get(purpose)
    if channel is not None and channel != target.channel:
        raise ValueError(f"{purpose!r} codes go by {channel} only")


def send_code(
    identifier: str | OtpTarget,
    purpose: str,
    *,
    ip: str,
    fid: str,
    lang: str,
    platform: str,
    social_jti: str | None = None,
) -> CodeIssue:
    """``auth/send-code/`` (customer contract §2.1.1), checks in contract order, for a phone or an e-mail.

    ``login_new_device`` is refused (only ``auth/login/`` issues it, after the password). Target state
    (``phone_registered`` / ``phone_not_registered``, ``email_registered`` / ``email_not_registered``), the
    channel's global hourly budget, then under the per-target advisory lock: lockout, daily wrong-code cap,
    resend window, target and fid day caps, IP day cap. For ``link_social`` the ticket's ``jti`` is remembered
    for the code (``gd:social:otp:{id}``, 15 min). ``attach_phone`` / ``verify_email`` (the signed-in
    ``me/phone/code/`` and ``me/email/code/``) check nothing about the target here: their caller refuses a
    phone or an address of another account first (``phone_taken`` / ``email_taken``).
    """
    if purpose not in OTP_PURPOSES or purpose == enums.OtpPurpose.LOGIN_NEW_DEVICE:
        raise ValueError(f"send_code cannot issue {purpose!r}")
    if purpose == enums.OtpPurpose.LINK_SOCIAL and not social_jti:
        raise ValueError("link_social codes need the ticket jti")
    target = otp_target(identifier)
    check_channel(purpose, target)
    customer = active_customer_by_phone(target)
    if purpose == enums.OtpPurpose.REGISTER and customer is not None:
        raise EmailRegistered() if target.is_email else PhoneRegistered()
    if purpose == enums.OtpPurpose.RESET_PASSWORD and customer is None:
        raise EmailNotRegistered() if target.is_email else PhoneNotRegistered()
    now = timeutils.now()
    budget = _budget_block(now, target.channel)
    if budget is not None:
        raise budget
    stored = OTP_PURPOSES[purpose]
    with transaction.atomic():
        _lock_target(target)
        block = _phone_block(target, now)
        if block is not None:
            raise block
        newest = target.codes().filter(purpose=stored).order_by("-created_at", "-id").first()
        resend = timedelta(seconds=int(settings.CUSTOMER_OTP_RESEND_SECONDS))
        if newest is not None and newest.created_at > now - resend:
            raise OtpResendTooSoon(retry_after=_seconds(newest.created_at + resend - now))
        block = _cap_block(target, fid=fid, ip=ip, now=now)
        if block is not None:
            raise block
        row = _create_code(
            target, stored, purpose=purpose, ip=ip, fid=fid, lang=lang, platform=platform, now=now
        )
        if social_jti:
            cache.set(social_otp_key(row.pk), social_jti, SOCIAL_OTP_TTL_SECONDS)
        return _issue(row, purpose, now)


def otp_blocked(identifier: str | OtpTarget, *, fid: str, ip: str, now: datetime) -> Exception | None:
    """The pre-password check of ``auth/login/`` step 3 (customer contract §2.1.4): takes the per-target
    advisory lock (held until the caller's transaction ends) and returns the refusal ``send_code`` could
    meet — lockout, daily code cap, daily wrong-code cap, fid cap, IP cap (``otp_network_limit``), the
    channel's global budget (``sms_unavailable`` / ``email_unavailable``) — or ``None``. The same answer for
    every password and for unknown phones and addresses."""
    target = otp_target(identifier)
    _lock_target(target)
    return (
        _phone_block(target, now)
        or _cap_block(target, fid=fid, ip=ip, now=now)
        or _budget_block(now, target.channel)
    )


def issue_login_code(
    identifier: str | OtpTarget, *, ip: str, fid: str, lang: str, platform: str
) -> CodeIssue:
    """The ``login_new_device`` code of ``auth/login/`` step 6, after the password check, sent to what the
    customer signed in with (phone → SMS, e-mail → e-mail).

    Runs under the advisory lock :func:`otp_blocked` took (the caller's transaction). A live code younger than
    the resend window is reused (nothing sent; ``resend_in`` = the remaining wait); otherwise a new one is
    issued. The fid, IP and global counters are incremented without a second check, so this never answers a
    cap error after the password was checked (a concurrent request may overshoot one of those cost caps by
    one).
    """
    target = otp_target(identifier)
    now = timeutils.now()
    _lock_target(target)
    stored = SmsCode.Purpose.NEW_DEVICE
    resend = timedelta(seconds=int(settings.CUSTOMER_OTP_RESEND_SECONDS))
    live = (
        target.codes()
        .filter(
            purpose=stored,
            consumed_at__isnull=True,
            exhausted_at__isnull=True,
            expires_at__gt=now,
            created_at__gt=now - resend,
        )
        .order_by("-created_at", "-id")
        .first()
    )
    if live is None:
        live = _create_code(
            target,
            stored,
            purpose=enums.OtpPurpose.LOGIN_NEW_DEVICE,
            ip=ip,
            fid=fid,
            lang=lang,
            platform=platform,
            now=now,
        )
    return _issue(live, enums.OtpPurpose.LOGIN_NEW_DEVICE, now)


def verify_code(identifier: str | OtpTarget, purpose: str, code: str) -> Verification:
    """``auth/verify-code/`` (customer contract §2.1.2) under the per-target advisory lock and a row lock.

    No live code (none, expired, consumed, superseded) → ``otp_expired``; exhausted row, lockout or daily
    wrong-code cap → ``otp_locked``; HMAC mismatch → ``attempts += 1`` (``exhausted_at`` at the max) is
    **committed** and then ``otp_invalid`` (``attempts_left`` = min(this code's, the day's)) or, at 0 left,
    ``otp_locked``. A match issues the single-use verification token (SHA-256 stored, 10 min).
    """
    target = otp_target(identifier)
    stored = OTP_PURPOSES[purpose]
    now = timeutils.now()
    refusal: Exception | None = None
    with transaction.atomic():
        _lock_target(target)
        row = target.codes().select_for_update().filter(purpose=stored).order_by("-created_at", "-id").first()
        if row is None or row.consumed_at is not None or row.expires_at <= now:
            raise OtpExpired()
        block = _phone_block(target, now)
        if row.exhausted_at is not None or block is not None:
            raise block or OtpLocked(retry_after=60 * int(settings.CUSTOMER_OTP_LOCKOUT_MINUTES))
        if not hmac.compare_digest(row.code_hmac, code_hmac(code)):
            row.attempts += 1
            if row.attempts >= row.max_attempts:
                row.exhausted_at = now
            row.save(update_fields=["attempts", "exhausted_at"])
            day_left = int(settings.CUSTOMER_OTP_DAILY_WRONG_LIMIT) - wrong_codes_today(target, now)
            left = min(row.max_attempts - row.attempts, day_left)
            if left <= 0:
                refusal = _phone_block(target, now) or OtpLocked(retry_after=_to_midnight(now))
            else:
                refusal = OtpInvalid(details={"attempts_left": left}, params={"n": left})
        else:
            token = secrets.token_urlsafe(32)
            ttl = int(settings.CUSTOMER_VERIFICATION_TTL_SECONDS)
            row.consumed_at = now
            row.verification_token_hash = hash_secret(token)
            row.verification_expires_at = now + timedelta(seconds=ttl)
            row.save(update_fields=["consumed_at", "verification_token_hash", "verification_expires_at"])
            return Verification(token, ttl, purpose, active_customer_by_phone(target) is not None)
    raise refusal


def verification_row(
    token: str,
    *,
    purpose: str,
    phone: str | None = None,
    lock: bool = False,
    email: str | None = None,
) -> SmsCode:
    """The usable ``SmsCode`` of a verification token (unused, unexpired, of ``purpose`` and, when given,
    of ``phone`` / ``email``), else 400 ``verification_invalid``. With ``lock`` the row is locked (``FOR
    UPDATE``) for :func:`consume_verification`."""
    if not token:
        raise VerificationInvalid()
    queryset = SmsCode.objects.filter(verification_token_hash=hash_secret(token))
    if lock:
        queryset = queryset.select_for_update()
    row = queryset.first()
    if (
        row is None
        or row.purpose != OTP_PURPOSES.get(purpose)
        or row.verification_used_at is not None
        or row.verification_expires_at is None
        or row.verification_expires_at <= timeutils.now()
        or (phone is not None and row.phone != phone)
        or (email is not None and row.email != normalize_email(email))
    ):
        raise VerificationInvalid()
    return row


def consume_verification(
    token: str, *, purpose: str, phone: str | None = None, email: str | None = None
) -> tuple[str, int]:
    """Mark a verification token used under its row lock (single use; part of the caller's transaction, so a
    refusal later in the flow rolls the use back). Returns ``(phone or e-mail address, sms_code_id)``."""
    if not transaction.get_connection().in_atomic_block:
        raise RuntimeError("consume_verification needs the caller's transaction")
    row = verification_row(token, purpose=purpose, phone=phone, email=email, lock=True)
    row.verification_used_at = timeutils.now()
    row.save(update_fields=["verification_used_at"])
    return row_identifier(row), row.pk


# --- rotating customer refresh (customer contract §2.1.6) ---


@dataclass(frozen=True, slots=True)
class RefreshResult:
    body: dict[str, Any]
    session_id: str


class RefreshUnknown(AuthFailed):
    """No session for the presented token (the view counts the miss in ``refresh_unknown``)."""

    def __init__(self) -> None:
        super().__init__("not_authenticated")


def _grace_key(digest: str) -> str:
    return REFRESH_GRACE_KEY.format(digest=digest)


def _dead_key(digest: str) -> str:
    return REFRESH_DEAD_KEY.format(digest=digest)


def refresh_body(session: AuthSession, raw_refresh: str) -> dict[str, Any]:
    access, expires = tokens.issue_access(session)
    return {
        "access": access,
        "access_expires_at": timeutils.isoformat(expires),
        "refresh": raw_refresh,
        "refresh_expires_at": timeutils.isoformat(session.absolute_expires_at),
    }


def _check_refresh_device(session: AuthSession, fid: str, platform: str | None) -> None:
    device = session.device
    if device is None or device.fid != fid or (platform is not None and device.platform != platform):
        raise AuthFailed("not_authenticated")


def rotate_customer_refresh(
    raw: str, *, fid: str, platform: str | None = None, request: Any = None
) -> RefreshResult:
    """``auth/refresh/`` steps 1–6 (customer contract §2.1.6) in one transaction on the locked row.

    Rotate (current token), grace replay (``auth:refresh_grace:{h}`` exists: identical tokens, nothing
    written, confirmed or not), re-rotation (previous token, rotation unconfirmed and younger than
    ``CUSTOMER_REFRESH_UNCONFIRMED_SECONDS``: the unused current hash is remembered as dead), else reuse → the
    session is revoked ``refresh_reused`` (committed, audited with the request) and 401 ``SESSION_EXPIRED``.
    The grace body is written before commit and deleted again when the transaction rolls back.

    Reuse is decided before the device check: a stale token (previous without a grace body outside the
    re-rotation window, or dead) revokes the session whichever install presents it — one from another
    install is the stronger theft signal, and its presenter gets nothing either way. Only the answers that
    hand out tokens (rotate, grace replay, re-rotation) require the session's device (else 401
    ``not_authenticated``).
    """
    digest = hash_secret(raw)
    written: list[str] = []
    refusal: Exception | None = None
    try:
        with transaction.atomic():
            session = (
                AuthSession.objects.select_for_update(of=("self",))
                .select_related("device", "customer")
                .filter(audience="customer")
                .filter(Q(refresh_hash=digest) | Q(previous_refresh_hash=digest))
                .first()
            )
            reuse = False
            if session is None:
                dead_sid = cache.get(_dead_key(digest))
                if not dead_sid:
                    raise RefreshUnknown()
                session = (
                    AuthSession.objects.select_for_update(of=("self",))
                    .select_related("device", "customer")
                    .filter(pk=dead_sid, audience="customer")
                    .first()
                )
                if session is None:
                    raise RefreshUnknown()
                reuse = True
            if session.revoked_at is not None:
                raise_revoked_session(session.revoke_reason)
            now = timeutils.now()
            customer = session.customer
            if (
                session.absolute_expires_at <= now
                or customer is None
                or customer.status != enums.CustomerStatus.ACTIVE
            ):
                raise AuthFailed("SESSION_EXPIRED")
            grace: Any = None
            window = timedelta(seconds=int(settings.CUSTOMER_REFRESH_UNCONFIRMED_SECONDS))
            unconfirmed = (
                not session.rotation_confirmed
                and session.rotated_at is not None
                and now - session.rotated_at <= window
            )
            if not reuse and session.refresh_hash != digest:
                grace = cache.get(_grace_key(digest))
                reuse = not isinstance(grace, dict) and not unconfirmed
            foreign_device = False
            try:
                _check_refresh_device(session, fid, platform)
            except AuthFailed:
                if not reuse:
                    raise
                foreign_device = True
            grace_ttl = int(settings.CUSTOMER_REFRESH_GRACE_SECONDS)
            if not reuse and session.refresh_hash == digest:
                new_raw = secrets.token_urlsafe(32)
                session.previous_refresh_hash = digest
                session.refresh_hash = hash_secret(new_raw)
                session.rotation += 1
                session.rotation_confirmed = False
                session.rotated_at = session.last_used_at = now
                session.absolute_expires_at = now + timedelta(days=int(settings.CUSTOMER_REFRESH_TTL_DAYS))
                session.save(
                    update_fields=[
                        "previous_refresh_hash",
                        "refresh_hash",
                        "rotation",
                        "rotation_confirmed",
                        "rotated_at",
                        "last_used_at",
                        "absolute_expires_at",
                    ]
                )
                body = refresh_body(session, new_raw)
                cache.set(_grace_key(digest), body, grace_ttl)
                written.append(_grace_key(digest))
                return RefreshResult(body, str(session.pk))
            if not reuse and isinstance(grace, dict):
                return RefreshResult(grace, str(session.pk))
            if not reuse:  # the previous token, no grace body, the rotation unconfirmed: re-rotate
                remaining = max(1, int((session.absolute_expires_at - now).total_seconds()))
                cache.set(_dead_key(session.refresh_hash), str(session.pk), remaining)
                written.append(_dead_key(session.refresh_hash))  # removed again on rollback
                new_raw = secrets.token_urlsafe(32)
                session.refresh_hash = hash_secret(new_raw)
                session.rotation += 1
                session.rotation_confirmed = False
                session.last_used_at = now
                session.save(update_fields=["refresh_hash", "rotation", "rotation_confirmed", "last_used_at"])
                body = refresh_body(session, new_raw)
                cache.set(_grace_key(digest), body, grace_ttl)
                written.append(_grace_key(digest))
                return RefreshResult(body, str(session.pk))
            # reuse: one of the holders is not the app (from this install or another one)
            _revoke_open_customer_sessions(
                AuthSession.objects.filter(pk=session.pk), enums.SessionRevokeReason.REFRESH_REUSED, at=now
            )
            _revoke_qr_on_commit(session.customer_id or 0)
            audit.record(
                action="customer.refresh_reused",
                entity_type="customer",
                entity_id=str(session.customer_id),
                actor=customer,
                request=request,
                after={"sid": str(session.pk)},
            )
            logger.warning("customer.refresh_reused sid=%s foreign_device=%s", session.pk, foreign_device)
            refusal = AuthFailed("SESSION_EXPIRED")
    except Exception:
        for key in written:
            cache.delete(key)
        raise
    raise refusal


def raise_revoked_session(reason: str) -> None:
    """The 401 of a revoked customer session (``replaced`` / ``password_changed`` / else expired)."""
    if reason == enums.SessionRevokeReason.REPLACED:
        raise AuthFailed("SESSION_REPLACED")
    if reason == enums.SessionRevokeReason.PASSWORD_CHANGED:
        raise PasswordChanged()
    raise AuthFailed("SESSION_EXPIRED")


@transaction.atomic
def logout_customer(raw: str, *, request: Any = None) -> AuthSession:
    """``auth/logout/`` (customer contract §2.1.7): revoke the session owning the refresh token (``logout``,
    tombstone on commit), the device inactive with no push and no token (its key hash kept), the customer's QR
    keys deleted on commit; audit ``customer.logout``. Unknown, revoked or expired → 401
    ``SESSION_EXPIRED``.

    Lock order Customer → session → device (C§2.7.1), as every other customer write: the session is first
    resolved without a lock, the customer-level lock taken, then the session re-read ``FOR UPDATE`` and
    re-checked (a sign-in, ``DELETE me/`` or ``PUT me/device/`` racing the logout serializes instead of
    deadlocking with it).
    """
    digest = hash_secret(raw)
    match = Q(refresh_hash=digest) | Q(previous_refresh_hash=digest)
    customer_id = (
        AuthSession.objects.filter(audience="customer")
        .filter(match)
        .values_list("customer_id", flat=True)
        .first()
    )
    if customer_id is None:
        raise AuthFailed("SESSION_EXPIRED")
    lock_customer(customer_id)
    session = (
        AuthSession.objects.select_for_update(of=("self",))
        .select_related("customer")
        .filter(audience="customer", customer_id=customer_id)
        .filter(match)
        .first()
    )
    if session is None or session.revoked_at is not None or session.absolute_expires_at <= timeutils.now():
        raise AuthFailed("SESSION_EXPIRED")
    _revoke_open_customer_sessions(
        AuthSession.objects.filter(pk=session.pk), enums.SessionRevokeReason.LOGOUT
    )
    if session.device_id is not None:
        Device.objects.filter(pk=session.device_id).update(
            is_active=False, push_enabled=False, fcm_token="", updated_at=timeutils.now()
        )
    _revoke_qr_on_commit(customer_id)
    audit.record(
        action="customer.logout",
        entity_type="customer",
        entity_id=str(customer_id),
        actor=session.customer,
        request=request,
        after={"sid": str(session.pk)},
    )
    return session


# --- social sign-in helpers (customer contract §2.1.8) ---


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def issue_social_nonce(fid: str) -> tuple[str, int]:
    """A 256-bit nonce bound to this install (``gd:social:nonce:{sha256(nonce)}`` = ``pii_ref(fid)``)."""
    nonce = secrets.token_urlsafe(32)
    ttl = int(settings.SOCIAL_NONCE_TTL_SECONDS)
    cache.set(SOCIAL_NONCE_KEY.format(digest=_sha256(nonce)), pii_ref(fid), ttl)
    return nonce, ttl


def consume_social_nonce(nonce: str, fid: str) -> bool:
    """Single use: the nonce must exist, be bound to ``fid``, and its deletion must succeed (a concurrent use
    of the same nonce deletes nothing and fails)."""
    if not nonce:
        return False
    key = SOCIAL_NONCE_KEY.format(digest=_sha256(nonce))
    if cache.get(key) != pii_ref(fid):
        return False
    return bool(cache.delete(key))


def remember_id_token(provider: str, raw_token: str, *, expires_at: int) -> bool:
    """The replay cache (``gd:social:seen:{sha256(provider + ":" + token)}`` until ``exp``); ``False``
    when the
    token was seen before. Keyed by the raw token: Apple's token has no ``jti`` and two tokens of one second
    share every claim."""
    ttl = max(1, int(expires_at - timeutils.now().timestamp()))
    return bool(cache.add(SOCIAL_SEEN_KEY.format(digest=_sha256(f"{provider}:{raw_token}")), 1, ttl))


# --- P1: customer writes the ``customers`` orchestrators make (AGENTS.md: cross-module writes go through the
# owning module's services). Every helper runs inside the caller's transaction, under its customer-level lock.


def lock_customer_by_phone(identifier: str | OtpTarget) -> Customer | None:
    """The customer-level lock of the row holding the phone — or, given an address, the e-mail — (any status),
    or ``None``."""
    return Customer.objects.select_for_update(no_key=True).filter(**otp_target(identifier).lookup).first()


class NoSession(enum.Enum):
    """The explicit "no session" of a signed-in write service called outside a customer request (staff,
    tasks): the session re-check is skipped only when a caller says so, never because ``sid`` was omitted."""

    NO_SESSION = "no_session"


NO_SESSION = NoSession.NO_SESSION


def lock_signed_in_customer(customer_id: int, sid: uuid.UUID | str | NoSession) -> Customer:
    """The customer-level lock of a signed-in write (customer contract §2.2, §6.17), re-checked under it.

    A request authenticated before a concurrent ``DELETE me/``, a reset or a sign-in elsewhere committed must
    not write to the account afterwards: the customer must still be ``active`` and the request's session
    (``sid``, required: :data:`NO_SESSION` only for a caller without a customer session) still open and
    unexpired, else 401 (``SESSION_REPLACED`` / ``password_changed`` for those
    revocations, ``SESSION_EXPIRED`` otherwise). The session row is locked (``FOR NO KEY UPDATE``) after the
    customer, the order ``start_customer_session`` uses, so a revocation either committed before (refused
    here) or waits for this write.
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
            raise_revoked_session(session.revoke_reason)
        if session.absolute_expires_at <= timeutils.now():
            raise AuthFailed("SESSION_EXPIRED")
    return stored


def create_customer(phone: str | None, *, name: str, language: str, email: str | None = None) -> Customer:
    """A new customer row (registration, social completion): with a verified phone, a verified e-mail
    (``email_verified_at`` = now), both, or neither (a social sign-up, hybrid identity). ``IntegrityError``
    when the phone or the address was taken meanwhile (inside its own savepoint, so the caller's transaction
    stays usable)."""
    address = normalize_email(email) if email else None
    with transaction.atomic():
        return Customer.objects.create(
            phone=phone or None,
            email=address,
            email_verified_at=timeutils.now() if address else None,
            name=name,
            language=language,
        )


def require_phone(customer: Customer) -> None:
    """The phone gate (hybrid identity, customer contract §2.2.10): checkout, pay, booking and the QR answer
    409 ``phone_required`` until the account has a verified phone (``me/phone/``). Every stored phone was
    verified by an SMS code (registration, social completion or attach), and a phone is never removed."""
    if not customer.phone:
        raise PhoneRequired()


def set_customer_phone(customer_id: int, phone: str) -> None:
    """Attach a verified phone to a phoneless account (hybrid identity, customer contract §2.2.10), under the
    caller's customer-level lock; ``IntegrityError`` (own savepoint) when another row holds it."""
    with transaction.atomic():
        Customer.objects.filter(pk=customer_id, phone__isnull=True).update(
            phone=phone, updated_at=timeutils.now()
        )


def set_customer_email(customer_id: int, email: str) -> None:
    """Set the verified e-mail address (replacing a previous one), ``email_verified_at`` = now, under the
    caller's customer-level lock; ``IntegrityError`` (own savepoint) when another row holds it."""
    instant = timeutils.now()
    with transaction.atomic():
        Customer.objects.filter(pk=customer_id).update(
            email=normalize_email(email), email_verified_at=instant, updated_at=instant
        )


EMAIL_NOTICE_AAD = b"email.notice"
EMAIL_NOTICE_KINDS = frozenset({"email_replaced"})


def enqueue_email_notice(address: str, *, lang: str, kind: str) -> None:
    """A security notice e-mail to ``address`` (hybrid identity: the previous address when it is replaced,
    C§2.2.10) through the outbox topic ``email.notice``; the address travels sealed (``core.secretbox``),
    never in clear in the payload."""
    if kind not in EMAIL_NOTICE_KINDS:
        raise ValueError(f"Unknown e-mail notice {kind!r}")
    outbox.enqueue(
        topics.EMAIL_NOTICE,
        {"to": secretbox.seal(address.encode("utf-8"), aad=EMAIL_NOTICE_AAD), "lang": lang, "kind": kind},
        dedupe_key=f"{topics.EMAIL_NOTICE}:{uuid.uuid4().hex}",
    )


def set_customer_language(customer_id: int, language: str) -> None:
    Customer.objects.filter(pk=customer_id).update(language=language, updated_at=timeutils.now())


def stamp_consent(customer_id: int, *, terms_version: str, ip: str) -> None:
    """Accept the terms (customer contract §2.1.3, §2.2.4): version, instant and client IP."""
    instant = timeutils.now()
    Customer.objects.filter(pk=customer_id).update(
        terms_version=terms_version, consent_at=instant, consent_ip=ip or None, updated_at=instant
    )


_KEEP: Any = object()


def update_customer_profile(
    customer_id: int, *, fid: str, name: str = _KEEP, language: str = _KEEP, push_enabled: bool = _KEEP
) -> None:
    """``PATCH me/``: name and language on the customer, ``push_enabled`` on the current device (field
    ``push_enabled`` ``push_token_missing`` without a stored FCM token)."""
    if push_enabled is not _KEEP:
        device = Device.objects.select_for_update().get(customer_id=customer_id, fid=fid)
        if push_enabled and not device.fcm_token:
            raise ValidationError(
                {"push_enabled": [field_error("push_token_missing", message_key="field.push_token_missing")]}
            )
        device.push_enabled = bool(push_enabled)
        device.save(update_fields=["push_enabled", "updated_at"])
    fields = {key: value for key, value in (("name", name), ("language", language)) if value is not _KEEP}
    if fields:
        Customer.objects.filter(pk=customer_id).update(**fields, updated_at=timeutils.now())


def clear_device_keys(customer_id: int, *, except_fid: str | None = None) -> int:
    """Forget every device key of the customer (but ``except_fid``'s): those installs need the SMS code on
    their next password sign-in (reset, dormant reset; customer contract §2.1.5, §2.2.3)."""
    queryset = Device.objects.filter(customer_id=customer_id)
    if except_fid is not None:
        queryset = queryset.exclude(fid=except_fid)
    return queryset.exclude(key_hash="").update(key_hash="", updated_at=timeutils.now())


def scrub_profile_for_dormant(customer_id: int, *, language: str) -> None:
    """The profile part of the dormant reset (customer contract §2.2.3, M2-A16): name, image (file deleted on
    commit), address text and addresses gone, the e-mail address too (hybrid identity: the previous holder of
    the number keeps no way in), language = the request language, consent cleared."""
    stored = Customer.objects.get(pk=customer_id)
    _delete_image_on_commit(stored)
    Customer.objects.filter(pk=customer_id).update(
        name="",
        image="",
        address_text="",
        email=None,
        email_verified_at=None,
        language=language,
        terms_version="",
        consent_at=None,
        consent_ip=None,
        updated_at=timeutils.now(),
    )
    Address.objects.filter(customer_id=customer_id).delete()


def scrub_deleted_for_reactivation(customer_id: int) -> None:
    """Backstop before a deleted row is reactivated (registration, social completion): whatever a write that
    raced ``DELETE me/`` left on it — password, image, e-mail, addresses, social identities (Apple revoked) —
    never
    reaches the next holder of the number (customer contract §6.17)."""
    stored = Customer.objects.get(pk=customer_id)
    if stored.status != enums.CustomerStatus.DELETED:
        return
    _delete_image_on_commit(stored)
    Customer.objects.filter(pk=customer_id).update(
        password="", image="", address_text="", email=None, email_verified_at=None, updated_at=timeutils.now()
    )
    Address.objects.filter(customer_id=customer_id).delete()
    for identity in list(CustomerSocialIdentity.objects.filter(customer_id=customer_id)):
        unlink_customer_identity(identity)


def set_customer_image(customer: Customer, filename: str, content: bytes) -> str:
    """``PUT me/image/`` on the locked ``customer``: store the processed file, delete the previous one on
    commit. Returns the new storage name."""
    previous = str(customer.image.name) if customer.image else ""
    storage = customer.image.storage
    customer.image.save(filename, ContentFile(content), save=True)
    new_name = str(customer.image.name)
    if previous and previous != new_name:
        transaction.on_commit(lambda: storage.delete(previous), robust=True)
    return new_name


def clear_customer_image(customer_id: int) -> None:
    """``DELETE me/image/``: the column cleared now, the file deleted on commit."""
    stored = Customer.objects.get(pk=customer_id)
    if stored.image:
        _delete_image_on_commit(stored)
        Customer.objects.filter(pk=customer_id).update(image="", updated_at=timeutils.now())


def link_customer_identity(
    customer_id: int,
    *,
    provider: str,
    subject: str,
    email: str,
    sealed_token: str = "",
    client_id: str = "",
    signed_in: bool = False,
) -> CustomerSocialIdentity:
    """Link a social identity (customer contract §2.1.9, §2.2.8). ``IntegrityError`` (inside its own
    savepoint) when the subject or the customer's provider slot was taken meanwhile."""
    with transaction.atomic():
        return CustomerSocialIdentity.objects.create(
            customer_id=customer_id,
            provider=provider,
            subject=subject,
            email_at_link=email[:254] if "@" in email else "",
            last_login_at=timeutils.now() if signed_in else None,
            provider_refresh_token=sealed_token,
            provider_client_id=client_id,
        )


def unlink_customer_identity(identity: CustomerSocialIdentity) -> None:
    """Delete one identity; an Apple identity holding a refresh token gets its ``apple.revoke``."""
    enqueue_apple_revoke(identity)
    identity.delete()


def touch_identity_sign_in(
    identity_id: int, customer_id: int, *, sealed_token: str = "", client_id: str = ""
) -> bool:
    """A linked identity signs in: lock it (after the customer-level lock), stamp ``last_login_at`` and store
    a newly exchanged Apple refresh token. ``False`` when the identity is gone (unlinked by a reset, the
    dormant rule or the customer meanwhile).

    A token exchanged while a concurrent sign-in of the same identity stored one first is dropped, not
    revoked: both belong to the same Apple authorization of the app, and Apple's revocation ends the whole
    authorization, so revoking the dropped one would invalidate the stored one too."""
    identity = (
        CustomerSocialIdentity.objects.select_for_update()
        .filter(pk=identity_id, customer_id=customer_id)
        .first()
    )
    if identity is None:
        return False
    fields: dict[str, Any] = {"last_login_at": timeutils.now()}
    if sealed_token and not identity.provider_refresh_token:
        fields.update(provider_refresh_token=sealed_token, provider_client_id=client_id)
    CustomerSocialIdentity.objects.filter(pk=identity_id).update(**fields)
    return True


def enqueue_apple_token_revoke(
    sealed_token: str, *, client_id: str, customer_id: int | None, subject: str
) -> bool:
    """Revoke an Apple refresh token the code exchange produced for a flow that was then refused (nothing
    linked): ``apple.revoke`` without an identity, deduplicated on the sealed token. Nothing is queued while
    an identity with the same ``subject`` is linked (:func:`apple_subject_linked`: revoking would end that
    identity's grant too); the handler re-checks at delivery. Returns whether a message was queued."""
    if not sealed_token or apple_subject_linked(subject):
        return False
    outbox.enqueue(
        topics.APPLE_REVOKE,
        {
            "identity_id": None,
            **apple_grant_reference(customer_id, subject),
            "client_id": client_id,
            "sealed_token": sealed_token,
        },
        dedupe_key=f"{topics.APPLE_REVOKE}:token:{hash_secret(sealed_token)[:32]}",
    )
    return True


def update_customer_address(address_id: int, customer_id: int, **changes: Any) -> Address:
    """``PATCH me/addresses/{id}/`` on the row re-read under the customer-level lock (404 when another
    request removed it meanwhile)."""
    return update_address(_locked_address(address_id, customer_id), **changes)


def set_customer_default_address(address_id: int, customer_id: int) -> Address:
    return set_default_address(_locked_address(address_id, customer_id))


def remove_customer_address(address_id: int, customer_id: int) -> None:
    remove_address(_locked_address(address_id, customer_id))


def _locked_address(address_id: int, customer_id: int) -> Address:
    address = Address.objects.select_for_update().filter(pk=address_id, customer_id=customer_id).first()
    if address is None:
        raise NotFound
    return address
