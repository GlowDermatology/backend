"""The campaign workflow actions: test, confirm the test, approve, return to draft, stop and cancel."""

from __future__ import annotations

from typing import Any, cast

from rest_framework.request import Request
from rest_framework.response import Response

from glowderma.accounts.api.base import AdminAPIView
from glowderma.accounts.permissions import IsSuperuser
from glowderma.core.idempotency import idempotent
from glowderma.notifications import services
from glowderma.notifications.api.admin.schemas.workflow import (
    NOTIFICATION_APPROVE_POST,
    NOTIFICATION_CANCEL_POST,
    NOTIFICATION_RETURN_TO_DRAFT_POST,
    NOTIFICATION_STOP_POST,
    NOTIFICATION_TEST_CONFIRM_POST,
    NOTIFICATION_TEST_POST,
)
from glowderma.notifications.api.admin.serializers import (
    ApproveInputSerializer,
    SendTestInputSerializer,
)
from glowderma.notifications.api.admin.views.common import record_body


class NotificationTestView(AdminAPIView):
    permission_classes = [IsSuperuser]

    @NOTIFICATION_TEST_POST
    @idempotent(required=False)
    def post(self, request: Request, pk: int) -> Response:
        serializer = SendTestInputSerializer(data=cast(dict[str, Any], request.data))
        serializer.is_valid(raise_exception=True)
        services.send_test(pk, serializer.validated_data["device_ids"], actor=request.user, request=request)
        return Response(record_body(pk))


class NotificationTestConfirmView(AdminAPIView):
    permission_classes = [IsSuperuser]

    @NOTIFICATION_TEST_CONFIRM_POST
    @idempotent(required=False)
    def post(self, request: Request, pk: int, test_id: int) -> Response:
        services.confirm_test(pk, test_id, actor=request.user, request=request)
        return Response(record_body(pk))


class NotificationApproveView(AdminAPIView):
    permission_classes = [IsSuperuser]

    @NOTIFICATION_APPROVE_POST
    @idempotent(required=False)
    def post(self, request: Request, pk: int) -> Response:
        serializer = ApproveInputSerializer(data=cast(dict[str, Any], request.data))
        serializer.is_valid(raise_exception=True)
        services.approve_campaign(
            pk,
            expected_revision=int(serializer.validated_data["expected_revision"]),
            actor=request.user,
            request=request,
        )
        return Response(record_body(pk))


class NotificationReturnToDraftView(AdminAPIView):
    permission_classes = [IsSuperuser]

    @NOTIFICATION_RETURN_TO_DRAFT_POST
    @idempotent(required=False)
    def post(self, request: Request, pk: int) -> Response:
        services.return_to_draft(pk, actor=request.user, request=request)
        return Response(record_body(pk))


class NotificationStopView(AdminAPIView):
    permission_classes = [IsSuperuser]

    @NOTIFICATION_STOP_POST
    @idempotent(required=False)
    def post(self, request: Request, pk: int) -> Response:
        services.request_stop(pk, actor=request.user, request=request)
        return Response(record_body(pk))


class NotificationCancelView(AdminAPIView):
    permission_classes = [IsSuperuser]

    @NOTIFICATION_CANCEL_POST
    @idempotent(required=False)
    def post(self, request: Request, pk: int) -> Response:
        services.cancel_campaign(pk, actor=request.user, request=request)
        return Response(record_body(pk))


__all__ = [
    "NotificationApproveView",
    "NotificationCancelView",
    "NotificationReturnToDraftView",
    "NotificationStopView",
    "NotificationTestConfirmView",
    "NotificationTestView",
]
