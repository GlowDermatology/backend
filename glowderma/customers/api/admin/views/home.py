"""The admin «Bosh sahifa» (admin contract §2.21): the landing sections staff can fill by hand (the spotlight,
the three rails, the categories, the specialists); an empty list makes a section automatic again."""

from __future__ import annotations

from rest_framework.exceptions import NotFound
from rest_framework.request import Request
from rest_framework.response import Response

from glowderma.accounts.api.base import AdminAPIView
from glowderma.core import enums
from glowderma.core.idempotency import idempotent
from glowderma.customers import selectors, services
from glowderma.customers.api.admin.schemas.home import HOME_SECTION_PUT, HOME_SECTIONS_GET
from glowderma.customers.api.admin.serializers import (
    HomeSectionSerializer,
    HomeSectionsSerializer,
    HomeSectionWriteSerializer,
)


class HomeSectionsView(AdminAPIView):
    @HOME_SECTIONS_GET
    def get(self, request: Request) -> Response:
        return Response(HomeSectionsSerializer({"results": selectors.home_sections()}).data)


class HomeSectionDetailView(AdminAPIView):
    @HOME_SECTION_PUT
    @idempotent(required=False)
    def put(self, request: Request, key: str) -> Response:
        if key not in enums.HomeSectionKey.values:
            raise NotFound()
        body = HomeSectionWriteSerializer(data=request.data)
        body.is_valid(raise_exception=True)
        services.save_home_section(key, body.validated_data["ids"], actor=request.user, request=request)
        return Response(HomeSectionSerializer(selectors.home_section(key)).data)


__all__ = [
    "HomeSectionDetailView",
    "HomeSectionsView",
]
