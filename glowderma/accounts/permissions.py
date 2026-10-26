from typing import TYPE_CHECKING

from rest_framework.permissions import BasePermission
from rest_framework.request import Request

from glowderma.accounts.models import Customer, StaffUser
from glowderma.core import enums

if TYPE_CHECKING:
    from rest_framework.views import APIView


class IsActiveStaff(BasePermission):
    def has_permission(self, request: Request, view: APIView) -> bool:
        return (
            isinstance(request.user, StaffUser)
            and request.user.is_active
            and isinstance(request.auth, dict)
            and request.auth.get("aud") == "staff"
        )


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
