"""The booking catalogue (O): specialists (list, detail, by slug) and services."""

from __future__ import annotations

from django.http import Http404
from rest_framework.request import Request
from rest_framework.response import Response

from glowderma.accounts.api.customer_base import PublicAPIView
from glowderma.appointments import selectors
from glowderma.appointments.api.customer.schemas.catalogue import (
    SERVICE_LIST_GET,
    SPECIALIST_BY_SLUG_GET,
    SPECIALIST_DETAIL_GET,
    SPECIALIST_LIST_GET,
)
from glowderma.appointments.api.customer.serializers import (
    ServiceListSerializer,
    SpecialistListSerializer,
    SpecialistSerializer,
)


class SpecialistListView(PublicAPIView):
    @SPECIALIST_LIST_GET
    def get(self, request: Request) -> Response:
        context = {"lang": self.language}
        return Response(SpecialistListSerializer({"results": selectors.specialists()}, context=context).data)


class SpecialistDetailView(PublicAPIView):
    @SPECIALIST_DETAIL_GET
    def get(self, request: Request, pk: int) -> Response:
        professional = selectors.specialist(pk)
        if professional is None:
            raise Http404
        return Response(SpecialistSerializer(professional, context={"lang": self.language}).data)


class SpecialistBySlugView(PublicAPIView):
    @SPECIALIST_BY_SLUG_GET
    def get(self, request: Request, slug: str) -> Response:
        professional = selectors.specialist_by_slug(slug)
        if professional is None:
            raise Http404
        return Response(SpecialistSerializer(professional, context={"lang": self.language}).data)


class ServiceListView(PublicAPIView):
    @SERVICE_LIST_GET
    def get(self, request: Request) -> Response:
        context = {"lang": self.language}
        return Response(ServiceListSerializer({"results": selectors.service_offers()}, context=context).data)


__all__ = [
    "ServiceListView",
    "SpecialistBySlugView",
    "SpecialistDetailView",
    "SpecialistListView",
]
