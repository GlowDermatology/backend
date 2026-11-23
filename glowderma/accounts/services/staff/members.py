"""Staff member management (admin contract §2.16; decisions GST-1..GST-6): create, edit, deactivate,
reactivate, reset a password and switch between the Staff and Specialist roles. At least one active
manager always remains.
"""

from __future__ import annotations

from typing import Any

from django.db import IntegrityError, transaction
from rest_framework.exceptions import NotFound, ValidationError

from glowderma.accounts import console_sessions
from glowderma.accounts.models import StaffUser
from glowderma.accounts.services.common import revocation
from glowderma.accounts.services.staff import auth_sessions, credentials, locks, messages, password_links
from glowderma.core import audit, enums
from glowderma.core.api.exceptions import Conflict, field_error
from glowderma.core.uzsource import N_

# Editable through ``PATCH staff/{id}/``. The email is the sign-in identity: changing it revokes the
# password links sent to the previous address.
STAFF_EDITABLE_FIELDS = ("full_name", "is_manager", "email")


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
    credentials.check_delivery(delivery)
    email = email.strip().lower()
    if StaffUser.objects.filter(email__iexact=email).exists():
        raise ValidationError({"email": [field_error("unique", messages.STAFF_EMAIL_TAKEN)]})
    manager = is_manager or is_superuser
    if role == enums.StaffRole.SPECIALIST and manager:
        raise ValueError("Specialist accounts cannot receive admin privileges")
    # Locked so two concurrent first accounts (or a concurrent demotion) see one another's result.
    if role == enums.StaffRole.ADMIN and not manager and not locks.lock_active_managers():
        manager = True
    user = StaffUser(email=email, full_name=full_name, is_manager=manager)
    raw = credentials.new_password(user, password, delivery)
    flagged = credentials.must_change(password, must_change)
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
        raise ValidationError({"email": [field_error("unique", messages.STAFF_EMAIL_TAKEN)]}) from exc
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
        password_links.issue_password_link(
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
    managers = locks.lock_active_managers() if role == enums.StaffRole.SPECIALIST else []
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
        raise Conflict(messages.STAFF_LAST_MANAGER)
    previous_role = user.role
    was_manager = user.is_manager
    user.role = role
    user.is_manager = False
    user.save(update_fields=["role", "is_manager"])
    sessions = auth_sessions.revoke_user_sessions_locked(user, enums.SessionRevokeReason.ADMIN_REVOKED)
    audit.record(
        action="staff.role_changed",
        entity_type="staff",
        entity_id=str(user.pk),
        actor=actor,
        request=request,
        before={"role": previous_role, "is_manager": was_manager},
        after={"role": user.role, "is_manager": False, "revoked_count": len(sessions)},
    )
    revocation.audit_revoked_sessions(sessions, actor=actor, request=request, reason="")
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
    managers = locks.lock_active_managers() if demoting else []
    stored = StaffUser.objects.select_for_update().get(pk=user.pk)
    before: dict[str, Any] = {}
    after: dict[str, Any] = {}
    if "email" in changes:
        changes = {**changes, "email": changes["email"].strip().lower()}
        if StaffUser.objects.filter(email__iexact=changes["email"]).exclude(pk=stored.pk).exists():
            raise ValidationError({"email": [field_error("unique", messages.STAFF_EMAIL_TAKEN)]})
    for name, value in changes.items():
        if getattr(stored, name) != value:
            before[name], after[name] = getattr(stored, name), value
    if after.get("is_manager") is True and stored.role == enums.StaffRole.SPECIALIST:
        raise Conflict(N_("Mutaxassis hisobi menejer bo‘la olmaydi."))
    if after.get("is_manager") is False:
        if stored.is_superuser:
            raise Conflict(messages.STAFF_SUPERUSER_MANAGER)
        if refuse_self and actor is not None and actor.pk == stored.pk:
            raise Conflict(messages.STAFF_SELF_DEMOTE)
        if stored.is_active and managers == [stored.pk]:
            raise Conflict(messages.STAFF_LAST_MANAGER)
    if not after:
        return stored
    for name, value in after.items():
        setattr(stored, name, value)
    try:
        with transaction.atomic():
            stored.save(update_fields=list(after))
    except IntegrityError as exc:  # lost a race with another account taking the same email
        raise ValidationError({"email": [field_error("unique", messages.STAFF_EMAIL_TAKEN)]}) from exc
    if "email" in after:
        # A link e-mailed to the previous login address no longer opens the account; the account keeps
        # its id, role, sessions and (for a specialist) its profile.
        password_links.revoke_password_tokens(stored)
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
        raise Conflict(messages.STAFF_SELF_DEACTIVATE)
    managers = locks.lock_active_managers()
    stored = StaffUser.objects.select_for_update().get(pk=user.pk)
    if not stored.is_active:
        user.is_active = False
        return 0
    if stored.is_manager and managers == [stored.pk]:
        raise Conflict(messages.STAFF_LAST_MANAGER)
    stored.is_active = False
    stored.save(update_fields=["is_active"])
    user.is_active = False
    sessions = auth_sessions.revoke_user_sessions_locked(stored, "deactivated")
    password_links.revoke_password_tokens(stored)
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
    revocation.audit_revoked_sessions(sessions, actor=actor, request=request, reason=reason)
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
        raise Conflict(messages.STAFF_SELF_RESET)
    credentials.check_delivery(delivery)
    stored = StaffUser.objects.select_for_update().get(pk=user.pk)
    if (
        protect_superuser
        and delivery != enums.StaffPasswordDelivery.EMAIL_LINK
        and not credentials.superuser_may_be_taken_by(stored, actor)
    ):
        raise Conflict(messages.STAFF_SUPERUSER_RESET)
    raw = credentials.new_password(stored, password, delivery)
    stored.set_password(raw)
    stored.must_change_password = credentials.must_change(password, must_change)
    stored.save(update_fields=["password", "must_change_password"])
    user.must_change_password = stored.must_change_password
    sessions = auth_sessions.revoke_user_sessions_locked(stored, "password_changed")
    ended_console_sessions = console_sessions.end_console_sessions(stored)
    password_links.revoke_password_tokens(stored)
    credentials.unlock_on_commit(stored.email)
    details: dict[str, Any] = {
        "revoked_count": len(sessions),
        "generated": raw is not None and password is None,
        "delivery": delivery,
        "must_change_password": stored.must_change_password,
    }
    if ended_console_sessions:
        details["console_sessions"] = ended_console_sessions
    audit.record(
        action="staff.password_reset",
        entity_type="staff",
        entity_id=str(stored.pk),
        actor=actor,
        request=request,
        after=details,
        reason=reason,
    )
    revocation.audit_revoked_sessions(sessions, actor=actor, request=request, reason=reason)
    if delivery != enums.StaffPasswordDelivery.TEMPORARY_PASSWORD:
        password_links.issue_password_link(
            stored, enums.StaffPasswordPurpose.RESET, actor=actor, request=request, via="reset", reason=reason
        )
    return raw, len(sessions)


def set_staff_language(user: StaffUser, language: str) -> None:
    """``PATCH auth/me/ {language}``: the admin panel's language of ``user`` (its e-mail links use it too)."""
    if language not in enums.ContentLanguage.values:
        raise ValueError(f"Unknown language {language!r}")
    StaffUser.objects.filter(pk=user.pk).update(language=language)
    user.language = language


__all__ = [
    "STAFF_EDITABLE_FIELDS",
    "create_staff",
    "change_specialist_role",
    "update_staff",
    "deactivate_staff",
    "reactivate_staff",
    "reset_staff_password",
    "set_staff_language",
]
