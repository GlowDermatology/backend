import hashlib
import logging
import secrets
import uuid
from datetime import timedelta
from typing import Any

from django.conf import settings
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import IntegrityError, transaction
from rest_framework.exceptions import ErrorDetail, NotFound, PermissionDenied, ValidationError

from glowderma.accounts import mfa, topics
from glowderma.accounts.api.guard import LoginGuard, email_hash
from glowderma.accounts.console import state as console_state
from glowderma.accounts.console import twofactor
from glowderma.accounts.errors import MfaChallengeInvalid, PasswordTokenInvalid
from glowderma.accounts.models import AuthSession, SocialIdentity, StaffPasswordToken, StaffUser
from glowderma.accounts.services.common import hashing as common_hashing
from glowderma.accounts.services.common import revocation as common_revocation
from glowderma.accounts.services.common.hashing import hash_secret as hash_secret
from glowderma.accounts.services.common.revocation import raise_revoked_session as raise_revoked_session
from glowderma.accounts.services.common.revocation import revoke_session as revoke_session
from glowderma.accounts.services.common.revocation import tombstone_ttl as tombstone_ttl
from glowderma.accounts.services.customer.addresses import add_address as add_address
from glowderma.accounts.services.customer.addresses import remove_address as remove_address
from glowderma.accounts.services.customer.addresses import remove_customer_address as remove_customer_address
from glowderma.accounts.services.customer.addresses import (
    set_customer_default_address as set_customer_default_address,
)
from glowderma.accounts.services.customer.addresses import set_default_address as set_default_address
from glowderma.accounts.services.customer.addresses import update_address as update_address
from glowderma.accounts.services.customer.addresses import update_customer_address as update_customer_address
from glowderma.accounts.services.customer.auth_sessions import SessionStart as SessionStart
from glowderma.accounts.services.customer.auth_sessions import customer_device_label as customer_device_label
from glowderma.accounts.services.customer.auth_sessions import logout_customer as logout_customer
from glowderma.accounts.services.customer.auth_sessions import (
    revoke_customer_sessions as revoke_customer_sessions,
)
from glowderma.accounts.services.customer.auth_sessions import revoke_session_rows as revoke_session_rows
from glowderma.accounts.services.customer.auth_sessions import sign_out_customer as sign_out_customer
from glowderma.accounts.services.customer.auth_sessions import sign_out_sessions as sign_out_sessions
from glowderma.accounts.services.customer.auth_sessions import (
    start_customer_session as start_customer_session,
)
from glowderma.accounts.services.customer.device_registration import (
    REGISTER_DEVICE_ATTEMPTS as REGISTER_DEVICE_ATTEMPTS,
)
from glowderma.accounts.services.customer.device_registration import register_device as register_device
from glowderma.accounts.services.customer.devices import DEVICE_KEY_HEADER_LENGTH as DEVICE_KEY_HEADER_LENGTH
from glowderma.accounts.services.customer.devices import clear_device_keys as clear_device_keys
from glowderma.accounts.services.customer.devices import (
    customers_with_sessionless_devices as customers_with_sessionless_devices,
)
from glowderma.accounts.services.customer.devices import device_is_known as device_is_known
from glowderma.accounts.services.customer.devices import invalidate_fcm_token as invalidate_fcm_token
from glowderma.accounts.services.customer.devices import (
    retire_sessionless_devices as retire_sessionless_devices,
)
from glowderma.accounts.services.customer.identities import SOCIAL_NONCE_KEY as SOCIAL_NONCE_KEY
from glowderma.accounts.services.customer.identities import SOCIAL_SEEN_KEY as SOCIAL_SEEN_KEY
from glowderma.accounts.services.customer.identities import apple_client_id as apple_client_id
from glowderma.accounts.services.customer.identities import apple_grant_reference as apple_grant_reference
from glowderma.accounts.services.customer.identities import apple_subject_linked as apple_subject_linked
from glowderma.accounts.services.customer.identities import apple_token_aad as apple_token_aad
from glowderma.accounts.services.customer.identities import consume_social_nonce as consume_social_nonce
from glowderma.accounts.services.customer.identities import enqueue_apple_revoke as enqueue_apple_revoke
from glowderma.accounts.services.customer.identities import (
    enqueue_apple_token_revoke as enqueue_apple_token_revoke,
)
from glowderma.accounts.services.customer.identities import issue_social_nonce as issue_social_nonce
from glowderma.accounts.services.customer.identities import link_customer_identity as link_customer_identity
from glowderma.accounts.services.customer.identities import open_apple_refresh as open_apple_refresh
from glowderma.accounts.services.customer.identities import remember_id_token as remember_id_token
from glowderma.accounts.services.customer.identities import seal_apple_refresh as seal_apple_refresh
from glowderma.accounts.services.customer.identities import touch_identity_sign_in as touch_identity_sign_in
from glowderma.accounts.services.customer.identities import (
    unlink_customer_identity as unlink_customer_identity,
)
from glowderma.accounts.services.customer.lifecycle import delete_customer as delete_customer
from glowderma.accounts.services.customer.lifecycle import reactivate_customer as reactivate_customer
from glowderma.accounts.services.customer.lifecycle import (
    scrub_deleted_for_reactivation as scrub_deleted_for_reactivation,
)
from glowderma.accounts.services.customer.lifecycle import (
    scrub_profile_for_dormant as scrub_profile_for_dormant,
)
from glowderma.accounts.services.customer.locks import NO_SESSION as NO_SESSION
from glowderma.accounts.services.customer.locks import NoSession as NoSession
from glowderma.accounts.services.customer.locks import lock_customer as lock_customer
from glowderma.accounts.services.customer.locks import lock_customer_by_phone as lock_customer_by_phone
from glowderma.accounts.services.customer.locks import lock_signed_in_customer as lock_signed_in_customer
from glowderma.accounts.services.customer.profile import EMAIL_NOTICE_AAD as EMAIL_NOTICE_AAD
from glowderma.accounts.services.customer.profile import EMAIL_NOTICE_KINDS as EMAIL_NOTICE_KINDS
from glowderma.accounts.services.customer.profile import check_customer_password as check_customer_password
from glowderma.accounts.services.customer.profile import clear_customer_image as clear_customer_image
from glowderma.accounts.services.customer.profile import create_customer as create_customer
from glowderma.accounts.services.customer.profile import enqueue_email_notice as enqueue_email_notice
from glowderma.accounts.services.customer.profile import move_history_horizon as move_history_horizon
from glowderma.accounts.services.customer.profile import require_phone as require_phone
from glowderma.accounts.services.customer.profile import set_customer_email as set_customer_email
from glowderma.accounts.services.customer.profile import set_customer_image as set_customer_image
from glowderma.accounts.services.customer.profile import set_customer_language as set_customer_language
from glowderma.accounts.services.customer.profile import set_customer_password as set_customer_password
from glowderma.accounts.services.customer.profile import set_customer_phone as set_customer_phone
from glowderma.accounts.services.customer.profile import stamp_consent as stamp_consent
from glowderma.accounts.services.customer.profile import update_customer_profile as update_customer_profile
from glowderma.accounts.services.customer.refresh import REFRESH_DEAD_KEY as REFRESH_DEAD_KEY
from glowderma.accounts.services.customer.refresh import REFRESH_GRACE_KEY as REFRESH_GRACE_KEY
from glowderma.accounts.services.customer.refresh import RefreshResult as RefreshResult
from glowderma.accounts.services.customer.refresh import RefreshUnknown as RefreshUnknown
from glowderma.accounts.services.customer.refresh import refresh_body as refresh_body
from glowderma.accounts.services.customer.refresh import rotate_customer_refresh as rotate_customer_refresh
from glowderma.accounts.services.otp.channels import CHANNEL_ONLY_PURPOSES as CHANNEL_ONLY_PURPOSES
from glowderma.accounts.services.otp.channels import EMAIL_CHANNEL as EMAIL_CHANNEL
from glowderma.accounts.services.otp.channels import SMS_CHANNEL as SMS_CHANNEL
from glowderma.accounts.services.otp.issuance import OTP_CODE_LENGTH as OTP_CODE_LENGTH
from glowderma.accounts.services.otp.issuance import OTP_PAYLOAD_KEY as OTP_PAYLOAD_KEY
from glowderma.accounts.services.otp.issuance import SOCIAL_OTP_KEY as SOCIAL_OTP_KEY
from glowderma.accounts.services.otp.issuance import SOCIAL_OTP_TTL_SECONDS as SOCIAL_OTP_TTL_SECONDS
from glowderma.accounts.services.otp.issuance import CodeIssue as CodeIssue
from glowderma.accounts.services.otp.issuance import code_hmac as code_hmac
from glowderma.accounts.services.otp.issuance import issue_login_code as issue_login_code
from glowderma.accounts.services.otp.issuance import otp_blocked as otp_blocked
from glowderma.accounts.services.otp.issuance import otp_payload_key as otp_payload_key
from glowderma.accounts.services.otp.issuance import send_code as send_code
from glowderma.accounts.services.otp.issuance import social_otp_key as social_otp_key
from glowderma.accounts.services.otp.limits import wrong_codes_today as wrong_codes_today
from glowderma.accounts.services.otp.targets import OTP_PURPOSES as OTP_PURPOSES
from glowderma.accounts.services.otp.targets import OtpTarget as OtpTarget
from glowderma.accounts.services.otp.targets import account_targets as account_targets
from glowderma.accounts.services.otp.targets import active_customer_by_email as active_customer_by_email
from glowderma.accounts.services.otp.targets import active_customer_by_phone as active_customer_by_phone
from glowderma.accounts.services.otp.targets import check_channel as check_channel
from glowderma.accounts.services.otp.targets import normalize_email as normalize_email
from glowderma.accounts.services.otp.targets import otp_target as otp_target
from glowderma.accounts.services.otp.targets import row_identifier as row_identifier
from glowderma.accounts.services.otp.targets import row_target as row_target
from glowderma.accounts.services.otp.verification import Verification as Verification
from glowderma.accounts.services.otp.verification import consume_verification as consume_verification
from glowderma.accounts.services.otp.verification import verification_row as verification_row
from glowderma.accounts.services.otp.verification import verify_code as verify_code
from glowderma.core import alerts, audit, enums, integration_modes, outbox, secretbox, timeutils
from glowderma.core.api.exceptions import AuthFailed, Conflict, field_error
from glowderma.core.uzsource import N_
from glowderma.integrations.oauth.ports import SocialClaims

logger = logging.getLogger(__name__)

SOCIAL_SUBJECT_LINKED = N_("Bu ijtimoiy hisob allaqachon bog‘langan.")
SOCIAL_PROVIDER_LINKED = N_("Bu provayder allaqachon bog‘langan.")


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
    common_revocation.tombstone_on_commit(sessions)
    return sessions


# --- staff management (admin contract §2.16; decisions GST-1..GST-6) ----------------------------------------

STAFF_EMAIL_TAKEN = N_("Bu email bilan xodim allaqachon bor (nofaol bo‘lsa, uni qayta faollashtiring).")
STAFF_LAST_MANAGER = N_("Kamida bitta faol menejer qolishi kerak. Avval boshqa xodimni menejer qiling.")
STAFF_SELF_DEACTIVATE = N_("O‘z hisobingizni faolsizlantira olmaysiz.")
STAFF_SELF_DEMOTE = N_("O‘zingizni menejerlikdan chiqara olmaysiz.")
STAFF_SUPERUSER_MANAGER = N_("Superadmin doim menejer bo‘ladi: uni menejerlikdan chiqarib bo‘lmaydi.")
STAFF_SELF_RESET = N_("O‘z parolingizni «Parolni o‘zgartirish» orqali almashtiring.")
STAFF_SUPERUSER_RESET = N_(
    "Superadminga vaqtinchalik parolni faqat superadmin bera oladi. Havolani uning e-pochtasiga yuboring "
    "yoki superadmindan so‘rang."
)
# Editable through ``PATCH staff/{id}/`` (the email is the sign-in identity and stays as created).
STAFF_EDITABLE_FIELDS = ("full_name", "is_manager", "email")
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
    delivery: str = enums.StaffPasswordDelivery.TEMPORARY_PASSWORD,
    language: str = enums.ContentLanguage.UZ,
    must_change: bool | None = None,
    role: enums.StaffRole = enums.StaffRole.ADMIN,
) -> tuple[StaffUser, str | None]:
    """Create an active staff account and return it with its temporary password (admin contract §2.16
    ``POST staff/``), ``None`` when ``delivery`` is ``email_link`` alone.

    ``delivery`` (feat/admin-ux, overriding GST-3): ``temporary_password`` generates a password
    (:func:`generate_temporary_password`) the caller shows once; ``email_link`` leaves the account without a
    usable password and e-mails an invite link (:func:`issue_password_link`); ``both`` does both. An e-mail
    delivery needs :func:`staff_email_invite_available` (else a ``delivery`` field error). Either way the
    account must set its own password first (``must_change_password``). ``manage.py createstaff`` passes the
    operator's validated ``password`` (no flag unless ``must_change``). The email is trimmed and
    lower-cased; an existing account with it (active or not, case-insensitive) is a ``unique`` field error.
    The first account of an install without an active manager becomes a manager, so staff management is
    never left without one (decision GST-1). A superuser is always a manager. Audited ``staff.created``
    (with ``delivery``), plus ``staff.password_link_sent`` for an e-mail delivery.
    """
    _check_delivery(delivery)
    email = email.strip().lower()
    if StaffUser.objects.filter(email__iexact=email).exists():
        raise ValidationError({"email": [field_error("unique", STAFF_EMAIL_TAKEN)]})
    manager = is_manager or is_superuser
    if role == enums.StaffRole.SPECIALIST and manager:
        raise ValueError("Specialist accounts cannot receive admin privileges")
    # Locked so two concurrent first accounts (or a concurrent demotion) see one another's result.
    if role == enums.StaffRole.ADMIN and not manager and not _lock_active_managers():
        manager = True
    user = StaffUser(email=email, full_name=full_name, is_manager=manager)
    raw = _new_password(user, password, delivery)
    flagged = _must_change(password, must_change)
    try:
        with transaction.atomic():
            create = StaffUser.objects.create_superuser if is_superuser else StaffUser.objects.create_user
            user = create(
                email,
                raw,
                full_name=full_name,
                is_manager=manager,
                role=role,
                must_change_password=flagged,
                language=language,
            )
    except IntegrityError as exc:  # lost a race with a concurrent create of the same email
        raise ValidationError({"email": [field_error("unique", STAFF_EMAIL_TAKEN)]}) from exc
    audit.record(
        action="staff.created",
        entity_type="staff",
        entity_id=str(user.pk),
        actor=actor,
        request=request,
        after={
            "email": user.email,
            "full_name": user.full_name,
            "is_manager": user.is_manager,
            "role": user.role,
            "delivery": delivery,
            "must_change_password": flagged,
        },
        reason=reason,
    )
    if delivery != enums.StaffPasswordDelivery.TEMPORARY_PASSWORD:
        issue_password_link(
            user, enums.StaffPasswordPurpose.INVITE, actor=actor, request=request, via="create", reason=reason
        )
    return user, raw


@transaction.atomic
def change_specialist_role(
    user_id: int,
    role: enums.StaffRole,
    *,
    actor: StaffUser,
    request: Any = None,
) -> StaffUser:
    """Switch one account between the Staff (``admin``) and Specialist roles; a Manager converted to
    Specialist loses the manager flag. Refused (409) for the caller's own account, an inactive account, a
    superuser, an account with operations-console access and the last active manager. The role the account
    already has is a no-op (a retried request), else the account's sessions are revoked and the change is
    audited ``staff.role_changed``. Only ``professionals`` calls this: it owns the specialist profile."""
    managers = _lock_active_managers() if role == enums.StaffRole.SPECIALIST else []
    user = StaffUser.objects.select_for_update().filter(pk=user_id).first()
    if user is None:
        raise NotFound
    if user.pk == actor.pk:
        raise Conflict(N_("O‘z hisobingiz rolini o‘zgartira olmaysiz."))
    if user.role == role:
        return user
    if not user.is_active:
        raise Conflict(N_("Rolni o‘zgartirishdan oldin hisobni faollashtiring."))
    if user.is_superuser:
        raise Conflict(N_("Superadmin hisobining rolini o‘zgartirib bo‘lmaydi."))
    if user.is_staff:
        raise Conflict(N_("Avval bu hisobning operatsiyalar konsoliga kirish huquqini olib tashlang."))
    if user.is_manager and not any(manager_id != user.pk for manager_id in managers):
        raise Conflict(STAFF_LAST_MANAGER)
    previous_role = user.role
    was_manager = user.is_manager
    user.role = role
    user.is_manager = False
    user.save(update_fields=["role", "is_manager"])
    sessions = _revoke_user_sessions(user, enums.SessionRevokeReason.ADMIN_REVOKED)
    audit.record(
        action="staff.role_changed",
        entity_type="staff",
        entity_id=str(user.pk),
        actor=actor,
        request=request,
        before={"role": previous_role, "is_manager": was_manager},
        after={"role": user.role, "is_manager": False, "revoked_count": len(sessions)},
    )
    common_revocation.audit_revoked_sessions(sessions, actor=actor, request=request, reason="")
    return user


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
    if "email" in changes:
        changes = {**changes, "email": changes["email"].strip().lower()}
        if StaffUser.objects.filter(email__iexact=changes["email"]).exclude(pk=stored.pk).exists():
            raise ValidationError({"email": [field_error("unique", STAFF_EMAIL_TAKEN)]})
    for name, value in changes.items():
        if getattr(stored, name) != value:
            before[name], after[name] = getattr(stored, name), value
    if after.get("is_manager") is True and stored.role == enums.StaffRole.SPECIALIST:
        raise Conflict(N_("Mutaxassis hisobi menejer bo‘la olmaydi."))
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
    try:
        with transaction.atomic():
            stored.save(update_fields=list(after))
    except IntegrityError as exc:  # lost a race with another account taking the same email
        raise ValidationError({"email": [field_error("unique", STAFF_EMAIL_TAKEN)]}) from exc
    if "email" in after:
        # A link e-mailed to the previous login address no longer opens the account; the account keeps
        # its id, role, sessions and (for a specialist) its profile.
        revoke_password_tokens(stored)
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
    revoke_password_tokens(stored)
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
    common_revocation.audit_revoked_sessions(sessions, actor=actor, request=request, reason=reason)
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


def _superuser_may_be_taken_by(user: StaffUser, actor: StaffUser | None) -> bool:
    """Whether ``actor`` may take over a credential of ``user`` (learn its password, strip its MFA):
    anyone for an ordinary account, only another superuser for a superuser (review SEC-2; the operator's
    commands are the break-glass path)."""
    return not user.is_superuser or (actor is not None and actor.is_superuser)


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
    delivery: str = enums.StaffPasswordDelivery.TEMPORARY_PASSWORD,
    must_change: bool | None = None,
    protect_superuser: bool = True,
) -> tuple[str | None, int]:
    """Replace ``user``'s password and revoke every open session (reason ``password_changed``).

    ``password=None`` generates a temporary password (returned once, never stored in clear or audited);
    ``manage.py resetstaffpassword --password-stdin`` passes the operator's validated one. ``delivery``
    (feat/admin-ux): ``email_link`` makes the password unusable and e-mails a reset link instead (returns
    ``None``), ``both`` does both; earlier links are revoked either way. A generated password or a link sets
    ``must_change_password`` (an operator's typed password only with ``must_change``). The caller's own
    password is refused (``refuse_self``, 409 ``conflict``): staff change it with ``auth/change-password/``,
    which asks for the current one, so a stolen session cannot take the account over. A superuser's password
    is handed (``temporary_password``, ``both``) only to another superuser (``protect_superuser``, 409
    ``conflict``; review SEC-2): a manager e-mails it the link instead, the operator commands pass ``False``.
    Social identities stay linked. Works on an inactive account too (prepared before a reactivation). The
    person's ops console sessions end too, a sign-in pending on the old password included
    (``console_sessions`` in the audit entry when there were any). Returns ``(password, revoked_count)``;
    audited ``staff.password_reset`` plus one ``auth.session_revoked`` per session. Once the reset commits, a
    running login lockout of the account ends (both :data:`STAFF_LOGIN_SCOPES`), for the API and ``manage.py
    resetstaffpassword`` alike.
    """
    if refuse_self and actor is not None and actor.pk == user.pk:
        raise Conflict(STAFF_SELF_RESET)
    _check_delivery(delivery)
    stored = StaffUser.objects.select_for_update().get(pk=user.pk)
    if (
        protect_superuser
        and delivery != enums.StaffPasswordDelivery.EMAIL_LINK
        and not _superuser_may_be_taken_by(stored, actor)
    ):
        raise Conflict(STAFF_SUPERUSER_RESET)
    raw = _new_password(stored, password, delivery)
    stored.set_password(raw)
    stored.must_change_password = _must_change(password, must_change)
    stored.save(update_fields=["password", "must_change_password"])
    user.must_change_password = stored.must_change_password
    sessions = _revoke_user_sessions(stored, "password_changed")
    console_sessions = console_state.end_console_sessions(stored)
    revoke_password_tokens(stored)
    _unlock_on_commit(stored.email)
    details: dict[str, Any] = {
        "revoked_count": len(sessions),
        "generated": raw is not None and password is None,
        "delivery": delivery,
        "must_change_password": stored.must_change_password,
    }
    if console_sessions:
        details["console_sessions"] = console_sessions
    audit.record(
        action="staff.password_reset",
        entity_type="staff",
        entity_id=str(stored.pk),
        actor=actor,
        request=request,
        after=details,
        reason=reason,
    )
    common_revocation.audit_revoked_sessions(sessions, actor=actor, request=request, reason=reason)
    if delivery != enums.StaffPasswordDelivery.TEMPORARY_PASSWORD:
        issue_password_link(
            stored, enums.StaffPasswordPurpose.RESET, actor=actor, request=request, via="reset", reason=reason
        )
    return raw, len(sessions)


# --- staff invite and forced password change (feat/admin-ux; admin contract §2.1, §2.16; overrides GST-3) ---

STAFF_EMAIL_INVITE_UNAVAILABLE = N_("E-pochta orqali yuborish sozlanmagan: vaqtinchalik parolni tanlang.")
STAFF_LINK_UNAVAILABLE = N_("E-pochta orqali yuborish sozlanmagan.")
STAFF_LINK_INACTIVE = N_("Nofaol hisobga havola yuborilmaydi. Avval uni qayta faollashtiring.")
STAFF_LINK_SELF = N_("O‘zingizga havola «Parolni o‘zgartirish» sahifasidan so‘raladi.")
STAFF_LINK_NOT_PENDING = N_("Parolingiz o‘rnatilgan: uni «Parolni o‘zgartirish» orqali almashtiring.")
PASSWORD_UNCHANGED = N_("Yangi parol eskisidan farq qilsin.")
# The sealed values of an ``email.staff_password_link`` payload open only for their own token id.
PASSWORD_LINK_AAD = b"email.staff_password_link"
PASSWORD_TOKEN_MAX_LENGTH = 128


def staff_email_invite_available() -> bool:
    """``Me.capabilities.staff_email_invite``: e-mail links can be sent — ``ADMIN_PANEL_URL`` is set and
    e-mail does not run its fake adapter under the production settings (C§6.14)."""
    return bool(settings.ADMIN_PANEL_URL) and not integration_modes.fake_in_production("email")


def _check_delivery(delivery: str) -> None:
    if delivery not in enums.StaffPasswordDelivery.values:
        raise ValueError(f"Unknown delivery {delivery!r}")
    if delivery != enums.StaffPasswordDelivery.TEMPORARY_PASSWORD and not staff_email_invite_available():
        raise ValidationError({"delivery": [field_error("invalid_choice", STAFF_EMAIL_INVITE_UNAVAILABLE)]})


def _new_password(user: StaffUser, password: str | None, delivery: str) -> str | None:
    """The operator's ``password``; else ``None`` (unusable) for an e-mail link alone; else a generated
    one."""
    if password is not None:
        return password
    if delivery == enums.StaffPasswordDelivery.EMAIL_LINK:
        return None
    return generate_temporary_password(user)


def _must_change(password: str | None, must_change: bool | None) -> bool:
    """Someone else chose the password (generated, or none yet): the person sets their own first."""
    return must_change if must_change is not None else password is None


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
        token_hash=common_hashing.hash_secret(raw),
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
    if not staff_email_invite_available():
        raise Conflict(STAFF_LINK_UNAVAILABLE)
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
        raise Conflict(STAFF_LINK_SELF)
    stored = StaffUser.objects.select_for_update().get(pk=user.pk)
    if not stored.is_active:
        raise Conflict(STAFF_LINK_INACTIVE)
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
        raise Conflict(STAFF_LINK_NOT_PENDING)
    return issue_password_link(
        stored, enums.StaffPasswordPurpose.RESET, actor=stored, request=request, via="self"
    )


def _password_token(raw: object) -> StaffPasswordToken | None:
    if not isinstance(raw, str) or not raw or len(raw) > PASSWORD_TOKEN_MAX_LENGTH:
        return None
    return (
        StaffPasswordToken.objects.select_related("staff")
        .filter(token_hash=common_hashing.hash_secret(raw))
        .first()
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
        raise ValidationError({"new_password": [ErrorDetail(PASSWORD_UNCHANGED, code="password_unchanged")]})
    instant = timeutils.now()
    user.set_password(new_password)
    user.must_change_password = False
    user.password_changed_at = instant
    user.save(update_fields=["password", "must_change_password", "password_changed_at"])
    row.used_at = instant
    row.save(update_fields=["used_at"])
    revoke_password_tokens(user, except_id=row.pk)
    sessions = _revoke_user_sessions(user, "password_changed")
    console_sessions = console_state.end_console_sessions(user)
    _unlock_on_commit(user.email)
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
    common_revocation.audit_revoked_sessions(sessions, actor=user, request=request, reason="")
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


def set_staff_language(user: StaffUser, language: str) -> None:
    """``PATCH auth/me/ {language}``: the admin panel's language of ``user`` (its e-mail links use it too)."""
    if language not in enums.ContentLanguage.values:
        raise ValueError(f"Unknown language {language!r}")
    StaffUser.objects.filter(pk=user.pk).update(language=language)
    user.language = language


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

    An account with the admin panel MFA on (feat/admin-ux) gets no session here: an automatic link is kept
    and :class:`~glowderma.accounts.mfa.MfaRequired` is raised with the sign-in challenge
    (``auth/mfa/verify/`` opens the session).
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
        if user is not None and identity is not None and mfa.enabled(user):
            challenge = mfa.issue_challenge(user, provider, identity_id=identity.pk, request=request)
        elif user is not None and identity is not None:
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
        else:
            challenge = None
    if challenge is not None:
        raise mfa.MfaRequired(challenge)  # after the commit: an automatic link stays
    # Outside the transaction so the audit row survives the 401 (the view's exception handler rolls back).
    audit.record(
        action="auth.login_failed",
        entity_type="staff",
        entity_id="",
        request=request,
        after={"email": claims.email, "method": provider},
    )
    raise AuthFailed("social_account_not_allowed")


# --- admin panel MFA: the sign-in's second step and the manager's reset (feat/admin-ux, accounts.mfa) ---


def complete_mfa_login(
    raw_challenge: object,
    *,
    code: str | None = None,
    recovery_code: str | None = None,
    device_label: str,
    request: Any = None,
) -> tuple[AuthSession, str]:
    """``auth/mfa/verify/``: the session of a sign-in that answered its challenge with a TOTP ``code`` or a
    ``recovery_code`` (``auth_method`` = the challenge's first step, ``mfa_verified_at`` stamped).

    400 ``mfa_challenge_invalid`` for an unknown, expired, used-up or exhausted challenge, or one whose
    account meanwhile changed its password, was deactivated or lost its panel MFA (the challenge is dropped);
    400 ``mfa_code_invalid`` for a wrong code (the challenge stays for the remaining attempts); 429 while the
    ``panel_otp`` lockout runs. The challenge is single use: claimed under the staff row lock right before the
    session opens. Audited ``auth.mfa_succeeded`` and ``auth.login_succeeded`` (``mfa``: the factor), plus
    ``auth.mfa_recovery_used`` (``remaining``) for a recovery code."""
    pending = mfa.open_challenge(raw_challenge)
    if pending is None:
        raise MfaChallengeInvalid
    user = StaffUser.objects.filter(pk=pending.staff_id).first()
    if user is None or not mfa.challenge_matches(pending, user):
        mfa.drop_challenge(pending)
        raise MfaChallengeInvalid
    # a running lockout answers 429 before the challenge's attempts are used
    mfa.check_attempt(user, request, reserve=False)
    if not mfa.count_attempt(pending):
        raise MfaChallengeInvalid
    used = mfa.use_factor(user, request, code=code, recovery_code=recovery_code, context="login")
    social = pending.method != AuthSession.AuthMethod.PASSWORD
    with transaction.atomic():
        locked = StaffUser.objects.select_for_update().get(pk=user.pk)
        # a social first step needs its identity still linked (an unlink since kills the challenge)
        unlinked = social and (
            pending.identity_id is None
            or not SocialIdentity.objects.filter(
                pk=pending.identity_id, staff=locked, provider=pending.method
            ).exists()
        )
        if unlinked:
            mfa.drop_challenge(pending)
            raise MfaChallengeInvalid
        if not mfa.challenge_matches(pending, locked) or not mfa.claim_challenge(pending):
            raise MfaChallengeInvalid
        session, refresh = start_staff_session(
            locked,
            device_label=device_label,
            auth_method=pending.method,
            failure_code="social_account_not_allowed" if social else "invalid_credentials",
        )
        instant = timeutils.now()
        session.mfa_verified_at = instant
        session.save(update_fields=["mfa_verified_at"])
        if social and pending.identity_id is not None:
            SocialIdentity.objects.filter(pk=pending.identity_id, staff=locked).update(last_login_at=instant)
        audit.record(
            action="auth.mfa_succeeded",
            entity_type="staff",
            entity_id=str(locked.pk),
            actor=locked,
            request=request,
            after={"factor": used.factor, "method": pending.method},
        )
        audit.record(
            action="auth.login_succeeded",
            entity_type="staff",
            entity_id=str(locked.pk),
            actor=locked,
            request=request,
            after={"email": locked.email, "method": pending.method, "mfa": used.factor},
        )
        if used.recovery_codes_left is not None:
            audit.record(
                action="auth.mfa_recovery_used",
                entity_type="staff",
                entity_id=str(locked.pk),
                actor=locked,
                request=request,
                after={"remaining": used.recovery_codes_left},
            )
    return session, refresh


MFA_SELF_RESET = N_("O‘z ikki bosqichli himoyangizni «Sozlamalar»da o‘chiring.")
MFA_NOT_ENABLED_FOR = N_("Bu xodimda ikki bosqichli himoya yoqilmagan.")
MFA_SUPERUSER_RESET = N_("Superadminning ikki bosqichli himoyasini faqat superadmin o‘chira oladi.")


@transaction.atomic
def reset_staff_mfa(
    user: StaffUser,
    *,
    actor: StaffUser | None,
    request: Any = None,
    reason: str = "",
    refuse_self: bool = True,
    via: str = "api",
    protect_superuser: bool = True,
) -> dict[str, int]:
    """A lost phone: delete ``user``'s admin panel TOTP devices and recovery codes (console devices stay),
    revoke every open session (``admin_revoked``) and end a running ``panel_otp`` lockout once it commits. 409
    for the caller's own account (``refuse_self``; they disable it in Settings), for an account without panel
    MFA, and for a superuser unless the caller is one too (``protect_superuser``; review SEC-2 — ``manage.py
    resetstaffmfa`` passes ``False``). Audited ``auth.mfa_reset`` (``via``: api, cli) plus
    ``auth.session_revoked`` per session; alerted ``auth.mfa_reset`` (ids only), since a manager who also
    resets the password could sign in as the person."""
    if refuse_self and actor is not None and actor.pk == user.pk:
        raise Conflict(MFA_SELF_RESET)
    stored = StaffUser.objects.select_for_update().get(pk=user.pk)
    if protect_superuser and not _superuser_may_be_taken_by(stored, actor):
        raise Conflict(MFA_SUPERUSER_RESET)
    panel = twofactor.PANEL
    if not panel.totp_devices(stored).exists() and not panel.recovery_devices(stored).exists():
        raise Conflict(MFA_NOT_ENABLED_FOR)
    removed = twofactor.reset_devices(stored, scope=twofactor.PANEL)
    sessions = _revoke_user_sessions(stored, "admin_revoked")
    hashed = email_hash(stored.email)

    def unlock() -> None:
        try:
            LoginGuard().unlock(mfa.GUARD_SCOPE, hashed)
        except Exception:
            logger.exception("auth.unlock_failed scope=%s", mfa.GUARD_SCOPE)

    transaction.on_commit(unlock, robust=True)
    details: dict[str, Any] = {**removed, "revoked_count": len(sessions), "via": via}
    audit.record(
        action="auth.mfa_reset",
        entity_type="staff",
        entity_id=str(stored.pk),
        actor=actor,
        request=request,
        after=details,
        reason=reason,
    )
    common_revocation.audit_revoked_sessions(sessions, actor=actor, request=request, reason=reason)
    logger.warning("auth.mfa_reset staff_id=%s via=%s", stored.pk, via)
    alerts.alert("auth.mfa_reset", {"staff_id": stored.pk, "via": via}, scope=str(stored.pk))
    return {**removed, "revoked_count": len(sessions)}


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


# --- customer sessions, devices and keys (M2 P0: customer contract §1.9, §2.1.6, §2.2.3, §2.2.6) ---
# P1 appends the OTP and credential flows below this block.


# --- P1: OTP, verification tokens, rotating refresh, logout, social nonce (customer contract §2.1) ---------
#
# Hybrid identity: one engine for codes sent by SMS (to a phone) and by e-mail (to an address). Every rule is
# keyed by the code's *target* (:class:`OtpTarget`): the per-target advisory lock, the lockout, the daily caps
# (codes and wrong codes), the resend window and the rows themselves; the fid and IP day counters are shared
# by both channels and each channel has its own global hourly breaker. Codes are stored as HMACs only.


# --- rotating customer refresh (customer contract §2.1.6) ---


# --- social sign-in helpers (customer contract §2.1.8) ---


# --- P1: customer writes the ``customers`` orchestrators make (AGENTS.md: cross-module writes go through the
# owning module's services). Every helper runs inside the caller's transaction, under its customer-level lock.
