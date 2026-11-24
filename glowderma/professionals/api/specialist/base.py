"""The base view of every ``specialist/`` route of ``professionals``: the caller's own active profile."""

from __future__ import annotations

from typing import Any

from rest_framework.exceptions import PermissionDenied
from rest_framework.request import Request

from glowderma.accounts.api.base import SpecialistAPIView
from glowderma.core.uzsource import N_
from glowderma.professionals import selectors
from glowderma.professionals.models import Professional


def require_active_profile(user_id: int, *, lock: bool = False) -> Professional:
    """The caller's own profile (draft or published). Every ``specialist/`` route resolves the profile from
    the signed-in account only, so a specialist can never address another specialist's rows."""
    professional = selectors.active_specialist_account(user_id, lock=lock)
    if professional is None:
        raise PermissionDenied(N_("Mutaxassis jadvali hozir mavjud emas."))
    return professional


class SpecialistProfileAPIView(SpecialistAPIView):
    account_id: int
    professional: Professional

    def initial(self, request: Request, *args: Any, **kwargs: Any) -> None:
        super().initial(request, *args, **kwargs)
        account_id = request.user.pk
        if not isinstance(account_id, int):
            raise PermissionDenied
        self.account_id = account_id
        self.professional = require_active_profile(account_id)


__all__ = [
    "SpecialistProfileAPIView",
    "require_active_profile",
]
