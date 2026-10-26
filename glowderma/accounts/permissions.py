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
