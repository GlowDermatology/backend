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
from django.core.cache import cache
from django.db import IntegrityError, transaction
from django.db.models import Q
from rest_framework.exceptions import NotFound, ValidationError

from glowderma.accounts import topics
from glowderma.accounts.models import (
    Address,
    AuthSession,
    Customer,
    CustomerSocialIdentity,
    Device,
    SocialIdentity,
    StaffUser,
)
from glowderma.core import audit, enums, outbox, qr_store, timeutils
from glowderma.core.api.exceptions import AuthFailed, Conflict, field_error
from glowderma.core.pii import pii_ref
from glowderma.integrations.oauth.ports import SocialClaims

logger = logging.getLogger(__name__)

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


def tombstone_ttl() -> int:
    """Seconds a ``revoked:<reason>`` tombstone outlives the last access token of either audience."""
    return max(int(settings.STAFF_ACCESS_TTL), int(settings.CUSTOMER_ACCESS_TTL)) + 60


def _tombstone_on_commit(sessions: Iterable[AuthSession]) -> None:
    values = {f"auth:sid:{session.pk}": f"revoked:{session.revoke_reason}" for session in sessions}
    if values:
        transaction.on_commit(lambda: cache.set_many(values, timeout=tombstone_ttl()))


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
def delete_customer(customer: Customer | int) -> Customer:
    """Soft-delete a customer (decision A1, customer contract §2.2.3 step 5): scrub, revoke, deactivate; keep
    the row.

    Under the customer-level lock (``FOR NO KEY UPDATE``, so a concurrent capture's ``Purchase`` insert does
    not wait): ``status=deleted``; name, image, address text, password scrubbed; ``language`` reset to ``uz``;
    consent (``terms_version``, ``consent_at``, ``consent_ip``) and ``last_login_at`` cleared (the audit log
    keeps the consent history); addresses and social identities deleted (one ``apple.revoke`` outbox message
    per Apple identity holding a token); every device inactive with no push, token or key hash; every session
    revoked ``deactivated`` (tombstones and the QR keys' deletion on commit). ``LoyaltyAccount``,
    ``Purchase``, ``Order`` and ``Appointment`` rows keep pointing at it (``PROTECT``). Idempotent: an already
    deleted customer is returned unchanged.
    """
    customer_id = customer if isinstance(customer, int) else customer.pk
    stored = lock_customer(customer_id)
    if stored.status == enums.CustomerStatus.DELETED:
        return _refreshed(customer, stored)
    instant = timeutils.now()
    if stored.image:
        stored.image.delete(save=False)
    stored.status = enums.CustomerStatus.DELETED
    stored.deleted_at = instant
    stored.name = ""
    stored.address_text = ""
    stored.image = ""
    stored.password = ""
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
    return _refreshed(customer, stored)


def _refreshed(customer: Customer | int, stored: Customer) -> Customer:
    if isinstance(customer, int):
        return stored
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


def apple_client_id() -> str:
    """The bundle id Apple tokens are revoked for: the first ``APPLE_BUNDLE_IDS`` entry, ``""`` when unset."""
    bundle_ids = list(settings.APPLE_BUNDLE_IDS)
    return str(bundle_ids[0]) if bundle_ids else ""


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
            "client_id": apple_client_id(),
            "sealed_token": identity.provider_refresh_token,
        },
        dedupe_key=topics.apple_revoke_key(identity.pk),
    )
    return True


@transaction.atomic
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

    Lock order (customer contract §2.7.1): the customer-level lock first, then the device rows — this
    device and the token's holders in one query ordered by primary key — so it serializes with
    ``start_customer_session`` and ``delete_customer`` instead of deadlocking with them. Under the lock a
    customer who is no longer active (401 ``SESSION_EXPIRED``) or a device another sign-in or a logout
    deactivated meanwhile (401 ``SESSION_REPLACED`` when its newest session was replaced, else
    ``SESSION_EXPIRED``) is refused before anything is written, so no token lands on a deleted or replaced
    device.
    """
    token = (fcm_token or "").strip()
    if push_enabled and not token:
        raise ValidationError(
            {"push_enabled": [field_error("push_token_missing", message_key="field.push_token_missing")]}
        )
    stored = lock_customer(customer_id)
    if stored.status != enums.CustomerStatus.ACTIVE:
        raise AuthFailed("SESSION_EXPIRED")
    instant = timeutils.now()
    wanted = Q(customer_id=customer_id, fid=fid)
    if token:
        wanted |= Q(fcm_token=token)
    rows = list(Device.objects.select_for_update().filter(wanted).order_by("pk"))
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
