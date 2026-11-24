"""What the admin ``catalog`` views share: the plain query dict and the viewset base that picks its parsers
per action."""

from __future__ import annotations

from typing import Any

from django.http import HttpRequest
from rest_framework.request import Request

from glowderma.accounts.api.base import AdminGenericViewSet


def query_params(request: Request) -> dict[str, str]:
    # A plain dict, not the QueryDict: DRF reads a missing BooleanField from HTML-style input as ``False``,
    # which would turn an absent ``promo`` / ``is_active`` filter into ``?promo=false``.
    return request.query_params.dict()


class ActionParsersViewSet(AdminGenericViewSet):
    """DRF picks the parsers before the viewset resolves ``self.action``: resolve it first, so ``get_parsers``
    can answer per action (multipart only where the contract allows it; anything else is a 415)."""

    def initialize_request(self, request: HttpRequest, *args: Any, **kwargs: Any) -> Request:
        action_map: dict[str, str] = getattr(self, "action_map", None) or {}
        self.action = action_map.get(request.method.lower() if request.method else "") or ""
        return super().initialize_request(request, *args, **kwargs)


__all__ = [
    "ActionParsersViewSet",
    "query_params",
]
