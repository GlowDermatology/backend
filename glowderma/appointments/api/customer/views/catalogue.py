"""The booking catalogue (O): specialists (list, detail, by slug) and services."""

from __future__ import annotations

from django.http import Http404
from rest_framework.request import Request
from rest_framework.response import Response

from glowderma.accounts.api.customer_base import PublicAPIView
from glowderma.appointments import selectors
from glowderma.appointments.api.customer.schemas.catalogue import (
    CLINIC_LIST_GET,
    SERVICE_LIST_GET,
    SPECIALIST_BY_SLUG_GET,
    SPECIALIST_DETAIL_GET,
    SPECIALIST_LIST_GET,
)
from glowderma.appointments.api.customer.serializers import (
    ClinicListSerializer,
    LocationSerializer,
    ServiceListSerializer,
    SpecialistListSerializer,
    SpecialistSerializer,
)


class SpecialistListView(PublicAPIView):
    public_cache_seconds = 60

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
    public_cache_seconds = 60

    @SERVICE_LIST_GET
    def get(self, request: Request) -> Response:
        context = {"lang": self.language}
        return Response(ServiceListSerializer({"results": selectors.service_offers()}, context=context).data)


class ClinicListView(PublicAPIView):
    public_cache_seconds = 60

    @CLINIC_LIST_GET
    def get(self, request: Request) -> Response:
        context = {"lang": self.language}
        results = [
            {
                **LocationSerializer(row.location, context=context).data,
                "specialist_count": row.specialist_count,
                "today_hours": (
                    None
                    if row.today_hours is None
                    else {"start": row.today_hours[0], "end": row.today_hours[1]}
                ),
                "open_now": row.open_now,
            }
            for row in selectors.clinics_today()
        ]
        return Response(ClinicListSerializer({"results": results}, context=context).data)


__all__ = [
    "ClinicListView",
    "ServiceListView",
    "SpecialistBySlugView",
    "SpecialistDetailView",
    "SpecialistListView",
]
