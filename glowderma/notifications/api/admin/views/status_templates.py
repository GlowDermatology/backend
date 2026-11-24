"""The status push templates."""

from __future__ import annotations

from typing import Any, cast

from rest_framework.exceptions import NotFound
from rest_framework.request import Request
from rest_framework.response import Response

from glowderma.accounts.api.base import AdminAPIView
from glowderma.core.idempotency import idempotent
from glowderma.notifications import selectors, services
from glowderma.notifications.api.admin.schemas.status_templates import TEMPLATE_GET, TEMPLATE_PUT
from glowderma.notifications.api.admin.serializers import (
    StatusTemplateSerializer,
    TemplateUpdateSerializer,
)


class TemplateView(AdminAPIView):
    def _template(self, status: str) -> Any:
        template = selectors.get_template(status)
        if template is None:
            raise NotFound
        return template

    @TEMPLATE_GET
    def get(self, request: Request, status: str) -> Response:
        return Response(StatusTemplateSerializer(self._template(status)).data)

    @TEMPLATE_PUT
    @idempotent(required=False)
    def put(self, request: Request, status: str) -> Response:
        self._template(status)
        serializer = TemplateUpdateSerializer(data=cast(dict[str, Any], request.data))
        serializer.is_valid(raise_exception=True)
        services.update_template(
            status, serializer.validated_data["texts"], actor=request.user, request=request
        )
        return Response(StatusTemplateSerializer(self._template(status)).data)


__all__ = [
    "TemplateView",
]
