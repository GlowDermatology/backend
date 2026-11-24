"""The admin banners: list (with its export), create, update, delete and order."""

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
from glowderma.catalog.api.admin.schemas.banners import (
    BANNER_CREATE,
    BANNER_DESTROY,
    BANNER_LIST,
    BANNER_PARTIAL_UPDATE,
    BANNER_REORDER,
    BANNER_RETRIEVE,
)
from glowderma.catalog.api.admin.serializers import (
    BannerListQuerySerializer,
    BannerPatchSerializer,
    BannerResultsSerializer,
    BannerSerializer,
    BannerWriteSerializer,
    IdOrderSerializer,
)
from glowderma.catalog.api.admin.views.common import ActionParsersViewSet, query_params
from glowderma.catalog.api.admin.views.forms import form_to_nested
from glowderma.catalog.models import Banner
from glowderma.core import enums, labels
from glowderma.core.api.exceptions import field_error
from glowderma.core.api.parsers import JSONParser
from glowderma.core.exports import ExportMixin, format_export_datetime
from glowderma.core.idempotency import idempotent
from glowderma.core.uzsource import N_, tr


class BannerViewSet(ExportMixin, mixins.ListModelMixin, ActionParsersViewSet):
    serializer_class = BannerSerializer
    export_name = N_("bannerlar")
    export_filter_keys = frozenset(BannerListQuerySerializer().fields)
    export_resource = "banners"
    export_headers = labels.BANNER_EXPORT_HEADERS

    def get_parsers(self) -> list[BaseParser]:
        # Contract §1.2 / §2.5: POST is multipart; PATCH takes multipart or JSON; the rest are JSON. Anything
        # else (``application/x-www-form-urlencoded`` included) is a 415.
        action = getattr(self, "action", None)
        if action == "create":
            return [MultiPartParser()]
        if action == "partial_update":
            return [JSONParser(), MultiPartParser()]
        return [JSONParser()]

    def get_queryset(self) -> QuerySet[Banner]:
        query = BannerListQuerySerializer(data=query_params(self.request))
        query.is_valid(raise_exception=True)
        qs = selectors.banner_queryset()
        if "is_active" in query.validated_data:
            qs = qs.filter(is_active=query.validated_data["is_active"])
        return qs

    def get_export_rows(self, items: Iterable[Banner]) -> Iterable[Sequence[Any]]:
        for banner in items:
            if banner.link_kind == enums.BannerLinkKind.PRODUCT and banner.link_product_id:
                link = tr(N_("mahsulot #%(id)s")) % {"id": banner.link_product_id}
            elif banner.link_kind == enums.BannerLinkKind.CATEGORY and banner.link_category_id:
                link = tr(N_("kategoriya %(slug)s")) % {
                    "slug": banner.link_category.slug if banner.link_category else ""
                }
            elif banner.link_kind == enums.BannerLinkKind.URL and banner.link_url:
                link = banner.link_url
            else:
                link = ""
            yield (
                banner.pk,
                banner.title_uz,
                link,
                labels.yes_no(banner.is_active),
                format_export_datetime(banner.starts_at),
                format_export_datetime(banner.ends_at),
                banner.position,
            )

    def _banner(self, pk: int) -> Banner:
        banner = selectors.banner_queryset().filter(pk=pk).first()
        if banner is None:
            raise NotFound
        return banner

    @BANNER_LIST
    def list(self, request: Request, *args: Any, **kwargs: Any) -> Response:
        return super().list(request, *args, **kwargs)

    @BANNER_CREATE
    @idempotent(required=False)
    def create(self, request: Request) -> Response:
        serializer = BannerWriteSerializer(data=form_to_nested(request.data))
        serializer.is_valid(raise_exception=True)
        image = request.FILES.get("image")
        if image is None:
            raise ValidationError({"image": [field_error("required")]})
        banner = services.create_banner(serializer.to_input(), image, actor=request.user, request=request)
        return Response(BannerSerializer(banner).data, status=201)

    @BANNER_RETRIEVE
    def retrieve(self, request: Request, pk: int) -> Response:
        return Response(BannerSerializer(self._banner(pk)).data)

    @BANNER_PARTIAL_UPDATE
    @idempotent(required=False)
    def partial_update(self, request: Request, pk: int) -> Response:
        banner = self._banner(pk)
        serializer = BannerPatchSerializer(instance=banner, data=form_to_nested(request.data), partial=True)
        serializer.is_valid(raise_exception=True)
        updated = services.update_banner(
            banner, serializer.to_input(), request.FILES.get("image"), actor=request.user, request=request
        )
        return Response(BannerSerializer(updated).data)

    @BANNER_DESTROY
    @idempotent(required=False)
    def destroy(self, request: Request, pk: int) -> Response:
        services.delete_banner(self._banner(pk), actor=request.user, request=request)
        return Response(status=204)

    @BANNER_REORDER
    @idempotent(required=False)
    def reorder(self, request: Request) -> Response:
        serializer = IdOrderSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        banners = services.reorder_banners(
            serializer.validated_data["ids"], actor=request.user, request=request
        )
        return Response(BannerResultsSerializer({"results": banners}).data)


__all__ = [
    "BannerViewSet",
]
