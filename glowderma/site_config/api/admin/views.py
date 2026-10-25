"""Admin views of `site_config`: ``GET`` / ``PATCH settings/`` (contract §2.11)."""

from typing import Any, cast

from drf_spectacular.utils import extend_schema
from rest_framework.request import Request
from rest_framework.response import Response

from glowderma.accounts.api.base import AdminAPIView
from glowderma.core.api.schema import ADMIN_ERRORS
from glowderma.core.idempotency import idempotent
from glowderma.site_config import services
from glowderma.site_config.api.admin.serializers import SettingsPatchSerializer, SettingsSerializer
from glowderma.site_config.models import SiteSettings


class SettingsView(AdminAPIView):
    @extend_schema(operation_id="admin_settings_retrieve", responses={200: SettingsSerializer, **ADMIN_ERRORS})
    def get(self, request: Request) -> Response:
        # Reads never create the singleton (post_migrate keeps it): a missing row is a deployment error.
        row = SiteSettings.objects.select_related("updated_by").get(pk=1)
        return Response(SettingsSerializer(row).data)

    @extend_schema(
        operation_id="admin_settings_partial_update",
        request=SettingsPatchSerializer,
        responses={200: SettingsSerializer, **ADMIN_ERRORS},
    )
    @idempotent(required=False)
    def patch(self, request: Request) -> Response:
        serializer = SettingsPatchSerializer(data=cast(dict[str, Any], request.data))
        serializer.is_valid(raise_exception=True)
        row = services.update_settings(serializer.to_changes(), actor=request.user, request=request)
        return Response(SettingsSerializer(row).data)
