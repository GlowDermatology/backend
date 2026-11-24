"""``notifications/meta/``: the options of the campaign editor."""

from __future__ import annotations

from rest_framework.request import Request
from rest_framework.response import Response

from glowderma.accounts.api.base import AdminAPIView
from glowderma.notifications import selectors
from glowderma.notifications.api.admin.schemas.meta import NOTIFICATION_META_GET
from glowderma.notifications.api.admin.serializers import (
    NotificationMetaSerializer,
)


class NotificationMetaView(AdminAPIView):
    @NOTIFICATION_META_GET
    def get(self, request: Request) -> Response:
        return Response(NotificationMetaSerializer(selectors.meta()).data)


__all__ = [
    "NotificationMetaView",
]
