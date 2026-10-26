"""Admin views of `site_config`: ``GET`` / ``PATCH settings/`` and ``POST settings/terms/publish/`` (contract
§2.11)."""

from typing import Any, cast

from drf_spectacular.utils import extend_schema
from rest_framework.request import Request
from rest_framework.response import Response

from glowderma.accounts.api.base import AdminAPIView
from glowderma.core.api.schema import ADMIN_ERRORS, IDEMPOTENCY_KEY_HEADER
from glowderma.core.idempotency import idempotent
from glowderma.site_config import services
from glowderma.site_config.api.admin.serializers import SettingsPatchSerializer, SettingsSerializer
from glowderma.site_config.models import SiteSettings


class SettingsView(AdminAPIView):
    @extend_schema(
        operation_id="admin_settings_retrieve", responses={200: SettingsSerializer, **ADMIN_ERRORS}
    )
    def get(self, request: Request) -> Response:
        # Reads never create the singleton (post_migrate keeps it): a missing row is a deployment error.
        row = SiteSettings.objects.select_related("updated_by").get(pk=1)
        return Response(SettingsSerializer(row).data)

    @extend_schema(
        operation_id="admin_settings_partial_update",
        description=(
            "Changes the sent fields only (each language of an i18n object separately). Saving the terms, "
            "privacy or booking texts does not publish them: customers see the saved text at once, but are "
            "asked to accept again only after `POST settings/terms/publish/`. While a version is published "
            "the uz terms and privacy texts cannot be emptied (`blank`)."
        ),
        request=SettingsPatchSerializer,
        responses={200: SettingsSerializer, **ADMIN_ERRORS},
    )
    @idempotent(required=False)
    def patch(self, request: Request) -> Response:
        serializer = SettingsPatchSerializer(data=cast(dict[str, Any], request.data))
        serializer.is_valid(raise_exception=True)
        row = services.update_settings(serializer.to_changes(), actor=request.user, request=request)
        return Response(SettingsSerializer(row).data)


class TermsPublishView(AdminAPIView):
    @extend_schema(
        operation_id="admin_settings_terms_publish",
        description=(
            "Publishes the current terms and privacy texts as a new `terms_version` (`YYYYMMDD-n`, the "
            "Tashkent day and its publication count) and sets `terms_updated_at`. Registration then accepts "
            "only the new version and every customer is asked to accept it again at checkout and booking "
            "(409 `terms_update_required`). 409 `conflict` while the uz terms or the uz privacy text is "
            "empty. Empty body."
        ),
        parameters=[IDEMPOTENCY_KEY_HEADER],
        request=None,
        responses={200: SettingsSerializer, **ADMIN_ERRORS},
    )
    @idempotent()
    def post(self, request: Request) -> Response:
        row = services.publish_terms(actor=request.user, request=request)
        return Response(SettingsSerializer(row).data)
