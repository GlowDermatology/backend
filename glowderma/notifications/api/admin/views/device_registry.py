"""The staff test devices: list, register and remove."""

from __future__ import annotations

from typing import Any, cast

from django.db.models import QuerySet
from rest_framework import mixins
from rest_framework.request import Request
from rest_framework.response import Response

from glowderma.accounts.api.base import AdminGenericViewSet
from glowderma.accounts.permissions import IsSuperuser
from glowderma.core.idempotency import idempotent
from glowderma.notifications import selectors, services
from glowderma.notifications.api.admin.schemas.device_registry import (
    TEST_DEVICE_CREATE,
    TEST_DEVICE_DESTROY,
    TEST_DEVICE_LIST,
)
from glowderma.notifications.api.admin.serializers import (
    TestDeviceCreateSerializer,
    TestDeviceSerializer,
)
from glowderma.notifications.models import TestDevice


class TestDeviceViewSet(mixins.ListModelMixin, AdminGenericViewSet):
    """``notifications/test-devices/`` (superuser): the global registry of real staff devices test pushes
    go to."""

    permission_classes = [IsSuperuser]
    serializer_class = TestDeviceSerializer
    pagination_class = None

    def get_queryset(self) -> QuerySet[TestDevice]:
        return selectors.test_devices()

    @TEST_DEVICE_LIST
    def list(self, request: Request, *args: Any, **kwargs: Any) -> Response:
        return Response(self.get_serializer(self.get_queryset(), many=True).data)

    @TEST_DEVICE_CREATE
    @idempotent(required=False)
    def create(self, request: Request) -> Response:
        serializer = TestDeviceCreateSerializer(data=cast(dict[str, Any], request.data))
        serializer.is_valid(raise_exception=True)
        test_device = services.register_test_device(
            phone=serializer.validated_data["phone"],
            label=serializer.validated_data["label"],
            actor=request.user,
            request=request,
        )
        return Response(TestDeviceSerializer(selectors.get_test_device(test_device.pk)).data, status=201)

    @TEST_DEVICE_DESTROY
    @idempotent(required=False)
    def destroy(self, request: Request, pk: int) -> Response:
        services.remove_test_device(pk, actor=request.user, request=request)
        return Response(status=204)


__all__ = [
    "TestDeviceViewSet",
]
