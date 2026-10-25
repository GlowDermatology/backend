import hashlib
import secrets
import uuid
from collections.abc import Iterable
from datetime import timedelta
from decimal import Decimal
from typing import Any

from django.conf import settings
from django.contrib.auth.hashers import check_password, make_password
from django.core.cache import cache
from django.db import IntegrityError, transaction
from rest_framework.exceptions import NotFound

from glowderma.accounts.models import (
    Address,
    AuthSession,
    Customer,
    CustomerSocialIdentity,
    Device,
    SocialIdentity,
    StaffUser,
)
from glowderma.core import audit, enums, timeutils
from glowderma.core.api.exceptions import AuthFailed, Conflict
from glowderma.integrations.oauth.ports import SocialClaims

SOCIAL_SUBJECT_LINKED = "Bu ijtimoiy hisob allaqachon bog‘langan."
SOCIAL_PROVIDER_LINKED = "Bu provayder allaqachon bog‘langan."


def start_staff_session(user: StaffUser, *, device_label: str) -> tuple[AuthSession, str]:
    refresh = secrets.token_urlsafe(32)
    session = AuthSession.objects.create(
        audience="staff",
        staff=user,
        device_label=device_label[:100],
        refresh_hash=hashlib.sha256(refresh.encode()).hexdigest(),
        absolute_expires_at=timeutils.now() + timedelta(seconds=settings.STAFF_REFRESH_TTL),
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


def _tombstone_on_commit(sessions: Iterable[AuthSession]) -> None:
    values = {f"auth:sid:{session.pk}": f"revoked:{session.revoke_reason}" for session in sessions}
    if values:
        transaction.on_commit(lambda: cache.set_many(values, timeout=settings.STAFF_ACCESS_TTL + 60))


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


@transaction.atomic
def deactivate_staff(user: StaffUser, *, actor: StaffUser, request: Any = None) -> int:
    """Deactivate ``user`` and revoke every open session with reason ``deactivated``.

    Returns the number of revoked sessions; an already inactive user revokes nothing (0).
    """
    stored = StaffUser.objects.select_for_update().get(pk=user.pk)
    if not stored.is_active:
        user.is_active = False
        return 0
    stored.is_active = False
    stored.save(update_fields=["is_active"])
    user.is_active = False
    sessions = _revoke_user_sessions(stored, "deactivated")
    audit.record(
        action="auth.staff_deactivated",
        entity_type="staff",
        entity_id=str(stored.pk),
        actor=actor,
        request=request,
        before={"is_active": True},
        after={"revoked_count": len(sessions)},
    )
    for session in sessions:
        audit.record(
            action="auth.session_revoked",
            entity_type="auth_session",
            entity_id=str(session.pk),
            actor=actor,
            request=request,
            after={"sid": str(session.pk), "reason": "deactivated"},
        )
    return len(sessions)


@transaction.atomic
def reactivate_staff(user: StaffUser, *, actor: StaffUser, request: Any = None) -> bool:
    """Reactivate ``user``. Returns False when the user was already active (no audit is written)."""
    stored = StaffUser.objects.select_for_update().get(pk=user.pk)
    if stored.is_active:
        user.is_active = True
        return False
    stored.is_active = True
    stored.save(update_fields=["is_active"])
    user.is_active = True
    audit.record(
        action="auth.staff_reactivated",
        entity_type="staff",
        entity_id=str(stored.pk),
        actor=actor,
        request=request,
        before={"is_active": False},
        after={"is_active": True},
    )
    return True


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
            session, refresh = start_staff_session(user, device_label=device_label)
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


@transaction.atomic
def link_identity(
    user: StaffUser, provider: str, claims: SocialClaims, *, request: Any = None
) -> SocialIdentity:
    """Link ``(provider, subject)`` to ``user`` (contract 2.1 ``POST auth/oauth/{provider}/link/``).

    One identity per ``(staff, provider)`` and per ``(provider, subject)``; either duplicate is a 409.
    """
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
    Customer.objects.select_for_update().get(pk=customer_id)
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
    Customer.objects.select_for_update().get(pk=address.customer_id)
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
    Customer.objects.select_for_update().get(pk=address.customer_id)
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
    Customer.objects.select_for_update().get(pk=customer_id)
    address.delete()
    _sync_address_text(customer_id)


def set_customer_password(customer: Customer, raw_password: str) -> None:
    customer.password = make_password(raw_password)
    customer.password_changed_at = timeutils.now()
    customer.save(update_fields=["password", "password_changed_at"])


def check_customer_password(customer: Customer, raw_password: str) -> bool:
    return bool(customer.password) and check_password(raw_password, customer.password)


@transaction.atomic
def delete_customer(customer: Customer) -> Customer:
    """Soft-delete a customer (decision A1): scrub PII, revoke sessions, deactivate devices; keep the row.

    ``LoyaltyAccount``, ``Purchase``, ``Order`` and ``Appointment`` rows keep pointing at it (``PROTECT``).
    Idempotent: an already deleted customer is returned unchanged.
    """
    stored = Customer.objects.select_for_update().get(pk=customer.pk)
    if stored.status == enums.CustomerStatus.DELETED:
        customer.refresh_from_db()
        return customer
    instant = timeutils.now()
    if stored.image:
        stored.image.delete(save=False)
    stored.status = enums.CustomerStatus.DELETED
    stored.deleted_at = instant
    stored.name = ""
    stored.address_text = ""
    stored.image = ""
    stored.password = ""
    stored.save(
        update_fields=["status", "deleted_at", "name", "address_text", "image", "password", "updated_at"]
    )
    Address.objects.filter(customer_id=stored.pk).delete()
    CustomerSocialIdentity.objects.filter(customer_id=stored.pk).delete()
    Device.objects.filter(customer_id=stored.pk).update(
        is_active=False, push_enabled=False, fcm_token="", updated_at=instant
    )
    sessions = list(
        AuthSession.objects.select_for_update().filter(customer_id=stored.pk, revoked_at__isnull=True)
    )
    if sessions:
        AuthSession.objects.filter(pk__in=[session.pk for session in sessions]).update(
            revoked_at=instant, revoke_reason="deactivated"
        )
        for session in sessions:
            session.revoked_at = instant
            session.revoke_reason = "deactivated"
        _tombstone_on_commit(sessions)
    customer.refresh_from_db()
    return customer


@transaction.atomic
def reactivate_customer(customer: Customer, *, name: str = "") -> Customer:
    """Re-registration with the same phone: ``active`` again, ``registered_at`` kept (first registration)."""
    stored = Customer.objects.select_for_update().get(pk=customer.pk)
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
