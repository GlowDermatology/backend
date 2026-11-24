"""OpenAPI declarations for A specialist's staff account: the invite and the role change."""

from __future__ import annotations

from drf_spectacular.utils import extend_schema

from glowderma.accounts.api.admin.serializers import (
    WorkforceInviteSerializer,
)
from glowderma.core.api.schema import ADMIN_ERRORS, IDEMPOTENCY_KEY_HEADER
from glowderma.professionals.api.admin.serializers.workforce import (
    SpecialistInviteCredentialsSerializer,
    SpecialistRoleChangeResultSerializer,
    SpecialistRoleChangeSerializer,
)

SPECIALIST_INVITE_POST = extend_schema(
    operation_id="admin_specialist_invite",
    description=(
        "Staff → Add employee → Specialist: creates the Specialist account and its private draft profile "
        "together (the profile is named after the account; the public name can be changed later). An "
        "email already in use → 400 `unique` on `email`, nothing is created. Resend the invitation with "
        "`staff/{id}/send-password-link/` or `staff/{id}/reset-password/`: same account, same profile."
    ),
    parameters=[IDEMPOTENCY_KEY_HEADER],
    request=WorkforceInviteSerializer,
    responses={201: SpecialistInviteCredentialsSerializer, **ADMIN_ERRORS},
)

SPECIALIST_ACCOUNT_ROLE_POST = extend_schema(
    operation_id="admin_staff_specialist_role",
    description=(
        "Staff → Edit: `specialist` converts an eligible Staff or Manager account (its draft profile is "
        "created, or the profile it already owns is reused unpublished); `staff` ends specialist access "
        "and unpublishes the profile, keeping it and its appointments on the account. Sessions are "
        "revoked on a change; the role the account already has changes nothing. The caller's own "
        "account, an inactive account, a superuser, operations-console access or the last active "
        "manager → 409 `conflict`."
    ),
    parameters=[IDEMPOTENCY_KEY_HEADER],
    request=SpecialistRoleChangeSerializer,
    responses={200: SpecialistRoleChangeResultSerializer, **ADMIN_ERRORS},
)


__all__ = [
    "SPECIALIST_ACCOUNT_ROLE_POST",
    "SPECIALIST_INVITE_POST",
]
