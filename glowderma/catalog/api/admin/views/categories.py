"""The admin categories: list (with its export), create, update, delete and order."""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from typing import Any

from django.db.models import QuerySet
from rest_framework import mixins
from rest_framework.exceptions import NotFound, ValidationError
from rest_framework.parsers import BaseParser, MultiPartParser
from rest_framework.request import Request
from rest_framework.response import Response

from glowderma.catalog import selectors, services
from glowderma.catalog.api.admin.schemas.categories import (
    CATEGORY_CREATE,
    CATEGORY_DESTROY,
    CATEGORY_LIST,
    CATEGORY_PARTIAL_UPDATE,
    CATEGORY_REORDER,
    CATEGORY_RETRIEVE,
)
from glowderma.catalog.api.admin.serializers import (
    CategoryListQuerySerializer,
    CategoryPatchSerializer,
    CategoryResultsSerializer,
    CategorySerializer,
    CategoryWriteSerializer,
    SlugOrderSerializer,
)
from glowderma.catalog.api.admin.views.common import ActionParsersViewSet, query_params
from glowderma.catalog.api.admin.views.forms import category_image, form_to_nested
from glowderma.catalog.models import Category
from glowderma.core import labels
from glowderma.core.api.exceptions import field_error
from glowderma.core.api.fields import RESERVED_SLUGS
from glowderma.core.api.parsers import JSONParser
from glowderma.core.exports import ExportMixin
from glowderma.core.idempotency import idempotent
from glowderma.core.media import absolute_media_url
from glowderma.core.uzsource import N_


class CategoryViewSet(ExportMixin, mixins.ListModelMixin, ActionParsersViewSet):
    serializer_class = CategorySerializer
    export_name = N_("kategoriyalar")
    export_filter_keys = frozenset(CategoryListQuerySerializer().fields)
    export_resource = "categories"
    export_headers = labels.CATEGORY_EXPORT_HEADERS
    lookup_field = "slug"

    def get_parsers(self) -> list[BaseParser]:
        # Gap ADM-5.1: create and PATCH take JSON or multipart (the image); the rest are JSON.
        if getattr(self, "action", None) in ("create", "partial_update"):
            return [JSONParser(), MultiPartParser()]
        return [JSONParser()]

    def get_queryset(self) -> QuerySet[Category]:
        query = CategoryListQuerySerializer(data=query_params(self.request))
        query.is_valid(raise_exception=True)
        qs = selectors.category_queryset()
        if "is_active" in query.validated_data:
            qs = qs.filter(is_active=query.validated_data["is_active"])
        return qs

    def get_export_rows(self, items: Iterable[Category]) -> Iterable[Sequence[Any]]:
        for category in items:
            yield (
                category.slug,
                category.name_uz,
                category.position,
                labels.yes_no(category.is_active),
                int(getattr(category, "product_count", 0)),
                absolute_media_url(category.image.name) if category.image.name else "",  # gap ADM-5.1
            )

    def _category(self, slug: str) -> Category:
        if slug in RESERVED_SLUGS:
            raise NotFound
        category = selectors.category_queryset().filter(slug=slug).first()
        if category is None:
            raise NotFound
        return category

    @CATEGORY_LIST
    def list(self, request: Request, *args: Any, **kwargs: Any) -> Response:
        return super().list(request, *args, **kwargs)

    @CATEGORY_CREATE
    @idempotent(required=False)
    def create(self, request: Request) -> Response:
        serializer = CategoryWriteSerializer(data=form_to_nested(request.data))
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        category = services.create_category(
            data["slug"],
            data["name"],
            is_active=data["is_active"],
            image=category_image(request),
            seo=data.get("seo"),
            actor=request.user,
            request=request,
        )
        return Response(CategorySerializer(category).data, status=201)

    @CATEGORY_RETRIEVE
    def retrieve(self, request: Request, slug: str) -> Response:
        return Response(CategorySerializer(self._category(slug)).data)

    @CATEGORY_PARTIAL_UPDATE
    @idempotent(required=False)
    def partial_update(self, request: Request, slug: str) -> Response:
        category = self._category(slug)
        serializer = CategoryPatchSerializer(
            instance=category, data=form_to_nested(request.data), partial=True
        )
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        image = category_image(request)
        remove_image = bool(data.get("remove_image", False))
        if image is not None and remove_image:
            raise ValidationError({"remove_image": [field_error("invalid")]})
        updated = services.update_category(
            category,
            name=data.get("name"),
            is_active=data.get("is_active"),
            image=image,
            remove_image=remove_image,
            seo=data.get("seo"),
            actor=request.user,
            request=request,
        )
        return Response(CategorySerializer(updated).data)

    @CATEGORY_DESTROY
    @idempotent(required=False)
    def destroy(self, request: Request, slug: str) -> Response:
        services.delete_category(self._category(slug), actor=request.user, request=request)
        return Response(status=204)

    @CATEGORY_REORDER
    @idempotent(required=False)
    def reorder(self, request: Request) -> Response:
        serializer = SlugOrderSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        categories = services.reorder_categories(
            serializer.validated_data["slugs"], actor=request.user, request=request
        )
        return Response(CategoryResultsSerializer({"results": categories}).data)


__all__ = [
    "CategoryViewSet",
]
