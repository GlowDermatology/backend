from typing import TYPE_CHECKING

from rest_framework.permissions import BasePermission
from rest_framework.request import Request

from glowderma.accounts.errors import PasswordChangeRequired
from glowderma.accounts.models import Customer, StaffUser
from glowderma.core import enums

if TYPE_CHECKING:
    from rest_framework.views import APIView

# A view that a staff member who must change their password may still use sets this attribute to ``True``
# (``auth/me/``, ``auth/change-password/``, ``auth/password-link/``).
ALLOW_PASSWORD_CHANGE_PENDING = "allow_password_change_pending"  # noqa: S105 - an attribute name


class IsActiveStaff(BasePermission):
    """An active staff member signed in with a staff access token.

    An account with ``must_change_password`` (a generated temporary password, an e-mail link; feat/admin-ux)
    gets 403 ``password_change_required`` from every view that does not set ``allow_password_change_pending``,
    whichever way it signed in (password or a social identity) and on every device."""

    def has_permission(self, request: Request, view: APIView) -> bool:
        allowed = (
            isinstance(request.user, StaffUser)
            and request.user.is_active
            and isinstance(request.auth, dict)
            and request.auth.get("aud") == "staff"
        )
        if (
            allowed
            and getattr(request.user, "must_change_password", False)
            and not getattr(view, ALLOW_PASSWORD_CHANGE_PENDING, False)
        ):
            raise PasswordChangeRequired
        return allowed


class IsActiveCustomer(BasePermission):
    """A customer signed in with a customer access token (``aud = customer``) whose account is active."""

    def has_permission(self, request: Request, view: APIView) -> bool:
        user: object = request.user  # typed as the staff user model by django-stubs
        return (
            isinstance(user, Customer)
            and user.status == enums.CustomerStatus.ACTIVE
            and isinstance(request.auth, dict)
            and request.auth.get("aud") == "customer"
        )


class IsManager(IsActiveStaff):
    """An active staff member signed in with a staff token whose account may manage staff (admin contract
    §2.16, ``StaffUser.is_manager``); anyone else gets 403 ``permission_denied`` before the handler runs.

    ``request.user`` is read from the database on every request (``StaffJWTAuthentication``), so a removed
    manager role takes effect on the next request. A superuser always passes (decision GST-1)."""

    def has_permission(self, request: Request, view: APIView) -> bool:
        user = request.user
        return super().has_permission(request, view) and bool(
            getattr(user, "is_manager", False) or getattr(user, "is_superuser", False)
        )


class IsSuperuser(IsActiveStaff):
    """An active staff member signed in with a staff token whose account is a superuser
    (``StaffUser.is_superuser``): the campaign approval routes of admin contract §2.8 (test, confirm,
    approve, return to draft, cancel, stop, test devices). Anyone else gets 403 ``permission_denied``
    before the handler runs; the flag is re-read from the database on every request."""

    def has_permission(self, request: Request, view: APIView) -> bool:
        return super().has_permission(request, view) and bool(getattr(request.user, "is_superuser", False))
