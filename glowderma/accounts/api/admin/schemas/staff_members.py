"""OpenAPI declarations for managing the staff directory (``staff/``, admin contract §2.16)."""

from __future__ import annotations

from drf_spectacular.utils import extend_schema

from glowderma.accounts.api.admin.serializers import (
    StaffCreateSerializer,
    StaffCredentialsSerializer,
    StaffLinkSentSerializer,
    StaffListItemSerializer,
    StaffListQuerySerializer,
    StaffMemberSerializer,
    StaffResetMfaSerializer,
    StaffResetPasswordSerializer,
    StaffUpdateSerializer,
)
from glowderma.core.api.schema import (
    ADMIN_ERRORS,
    IDEMPOTENCY_KEY_HEADER,
    OPTIONAL_IDEMPOTENCY_KEY_HEADER,
)

STAFF_ID_DESCRIPTION = "Manager only (403 `permission_denied` otherwise)."

STAFF_LIST = extend_schema(
    operation_id="admin_staff_list",
    description=(
        "Every staff member: the directory behind the audit-log actor filter. A manager's rows also "
        "carry `full_name`, `is_manager`, `last_login_at`, `linked_providers`, `created_at` and "
        "(feat/admin-ux) `must_change_password`, `mfa_enabled` and `invite`; for anyone else these keys "
        "are `null`."
    ),
    parameters=[StaffListQuerySerializer],
    responses={200: StaffListItemSerializer(many=True), **ADMIN_ERRORS},
)

STAFF_CREATE = extend_schema(
    operation_id="admin_staff_create",
    description=(
        f"{STAFF_ID_DESCRIPTION} Creates an active staff account. `delivery`: `temporary_password` "
        "(generated, shown once in the answer), `email_link` (an invite link e-mailed to the account; no "
        "usable password until it is used) or `both`; either way the person must set their own password "
        "first (`must_change_password`). `email` already used by any account → `unique`; an e-mail "
        "delivery while `capabilities.staff_email_invite` is false → `delivery` `invalid_choice`."
    ),
    parameters=[IDEMPOTENCY_KEY_HEADER],
    request=StaffCreateSerializer,
    responses={201: StaffCredentialsSerializer, **ADMIN_ERRORS},
)

STAFF_RETRIEVE = extend_schema(
    operation_id="admin_staff_retrieve",
    description=STAFF_ID_DESCRIPTION,
    responses={200: StaffMemberSerializer, **ADMIN_ERRORS},
)

STAFF_PARTIAL_UPDATE = extend_schema(
    operation_id="admin_staff_partial_update",
    description=(
        f"{STAFF_ID_DESCRIPTION} Changes `full_name` and/or `is_manager` (`email`, `is_active`, "
        "`password` → `read_only`). Removing the manager role of the caller or of the last active "
        "manager → 409 `conflict`."
    ),
    parameters=[OPTIONAL_IDEMPOTENCY_KEY_HEADER],
    request=StaffUpdateSerializer,
    responses={200: StaffMemberSerializer, **ADMIN_ERRORS},
)

STAFF_DEACTIVATE = extend_schema(
    operation_id="admin_staff_deactivate",
    description=(
        f"{STAFF_ID_DESCRIPTION} The account can no longer sign in: every open session is revoked "
        "(`deactivated`), linked identities stay. The caller's own account or the last active manager → "
        "409 `conflict`; an inactive account → 200 unchanged."
    ),
    parameters=[IDEMPOTENCY_KEY_HEADER],
    request=None,
    responses={200: StaffMemberSerializer, **ADMIN_ERRORS},
)

STAFF_REACTIVATE = extend_schema(
    operation_id="admin_staff_reactivate",
    description=(
        f"{STAFF_ID_DESCRIPTION} The account may sign in again (password, identities and manager role "
        "kept); an active account → 200 unchanged."
    ),
    parameters=[OPTIONAL_IDEMPOTENCY_KEY_HEADER],
    request=None,
    responses={200: StaffMemberSerializer, **ADMIN_ERRORS},
)

STAFF_RESET_PASSWORD = extend_schema(
    operation_id="admin_staff_reset_password",
    description=(
        f"{STAFF_ID_DESCRIPTION} Replaces the password and revokes every open session "
        "(`password_changed`) and every earlier link; linked identities stay. `delivery` (optional body, "
        "default `temporary_password`): a generated temporary password (shown once), `email_link` (the "
        "password becomes unusable and a reset link is e-mailed) or `both`; the person must then set "
        "their own password. The caller's own account → 409 `conflict` (use `auth/change-password/`); a "
        "superuser's account with `temporary_password` or `both`, unless the caller is a superuser too → "
        "409 `conflict` (send `email_link`)."
    ),
    parameters=[IDEMPOTENCY_KEY_HEADER],
    request=StaffResetPasswordSerializer,
    responses={200: StaffCredentialsSerializer, **ADMIN_ERRORS},
)

STAFF_SEND_PASSWORD_LINK = extend_schema(
    operation_id="admin_staff_send_password_link",
    description=(
        f"{STAFF_ID_DESCRIPTION} E-mails the account a new single-use link to set its password (invite "
        "for an account that never signed in nor set one, else reset); the password and the sessions are "
        "not changed, earlier links are revoked. The caller's own account, an inactive account, or "
        "e-mail links not available → 409 `conflict`; 5/hour per manager and account."
    ),
    parameters=[OPTIONAL_IDEMPOTENCY_KEY_HEADER],
    request=None,
    responses={200: StaffLinkSentSerializer, **ADMIN_ERRORS},
)

STAFF_RESET_MFA = extend_schema(
    operation_id="admin_staff_reset_mfa",
    description=(
        f"{STAFF_ID_DESCRIPTION} A lost phone: removes the account's admin panel MFA (TOTP device and "
        "recovery codes; ops console devices stay) and revokes every open session (`admin_revoked`). "
        "The caller's own account, an account without panel MFA, or a superuser's account unless the "
        "caller is a superuser too (`manage.py resetstaffmfa` stays the break-glass path) → 409 "
        "`conflict`. Optional body "
        "`reason` (trimmed, at most 200 characters, blank allowed) is kept on the audit entry. Audited "
        "`auth.mfa_reset` and alerted."
    ),
    parameters=[OPTIONAL_IDEMPOTENCY_KEY_HEADER],
    request=StaffResetMfaSerializer,
    responses={200: StaffMemberSerializer, **ADMIN_ERRORS},
)


__all__ = [
    "STAFF_CREATE",
    "STAFF_DEACTIVATE",
    "STAFF_ID_DESCRIPTION",
    "STAFF_LIST",
    "STAFF_PARTIAL_UPDATE",
    "STAFF_REACTIVATE",
    "STAFF_RESET_MFA",
    "STAFF_RESET_PASSWORD",
    "STAFF_RETRIEVE",
    "STAFF_SEND_PASSWORD_LINK",
]
