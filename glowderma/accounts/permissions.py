from typing import TYPE_CHECKING

from rest_framework.permissions import BasePermission
from rest_framework.request import Request

from glowderma.accounts.models import StaffUser

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
