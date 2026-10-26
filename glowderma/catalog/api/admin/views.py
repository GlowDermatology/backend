"""Admin views of `catalog` (contract §2.3 Products and Billz, §2.4 Categories, §2.5 Banners)."""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from typing import Any, cast

from django.core.files.uploadedfile import UploadedFile
from django.db.models import QuerySet
from django.http import HttpRequest, QueryDict
from drf_spectacular.utils import extend_schema, inline_serializer
from rest_framework import mixins, serializers
from rest_framework.exceptions import NotFound, ValidationError
from rest_framework.parsers import BaseParser, MultiPartParser
from rest_framework.request import Request
from rest_framework.response import Response

from glowderma.accounts.api.base import AdminAPIView, AdminGenericViewSet
from glowderma.catalog import billz, selectors, services
from glowderma.catalog.api.admin.serializers import (
    BannerListQuerySerializer,
    BannerPatchSerializer,
    BannerResultsSerializer,
    BannerSerializer,
    BannerWriteSerializer,
    CategoryListQuerySerializer,
    CategoryResultsSerializer,
    CategorySerializer,
    CategoryWriteSerializer,
    IdOrderSerializer,
    ImageOrderSerializer,
    ImagesResponseSerializer,
    ProductImageSerializer,
    ProductListQuerySerializer,
    ProductPatchSerializer,
    ProductSerializer,
    ProductStatsSerializer,
    ProductWriteSerializer,
    SlugOrderSerializer,
)
from glowderma.catalog.models import Banner, Category, Product
from glowderma.core import enums, labels
from glowderma.core.api.exceptions import field_error
from glowderma.core.api.fields import RESERVED_SLUGS
from glowderma.core.api.parsers import JSONParser
from glowderma.core.api.schema import ADMIN_ERRORS, EXPORT_PARAM
from glowderma.core.exports import ExportMixin, format_export_datetime
from glowderma.core.idempotency import idempotent
from glowderma.integrations import selectors as integration_selectors
from glowderma.integrations import services as job_services
from glowderma.integrations.api.admin.serializers import BillzImportSerializer, JobSerializer

# --- products -----------------------------------------------------------------------------------------------


class ProductViewSet(ExportMixin, mixins.ListModelMixin, AdminGenericViewSet):
    serializer_class = ProductSerializer
    page_size_options = (8,)
    default_page_size = 8
    export_name = "mahsulotlar"
    export_filter_keys = frozenset(ProductListQuerySerializer().fields)
    export_resource = "products"
    export_headers = labels.PRODUCT_EXPORT_HEADERS

    def get_queryset(self) -> QuerySet[Product]:
        query = ProductListQuerySerializer(data=query_params(self.request))
        query.is_valid(raise_exception=True)
        filters = cast(selectors.ProductFilters, dict(query.validated_data))
        return selectors.filter_products(selectors.product_queryset(), filters)

    def get_export_rows(self, items: Iterable[Product]) -> Iterable[Sequence[Any]]:
        for product in items:
            yield (
                product.pk,
                product.billz_id,
                product.billz_name,
                product.name_uz,
                product.category.name_uz if product.category_id and product.category else "",
                product.price,
                int(getattr(product, "stock_quantity", 0)),
                labels.yes_no(product.promo),
                labels.PRODUCT_STATUS[product.status],
                labels.yes_no(product.is_new_from_billz),
            )

    @extend_schema(
        operation_id="admin_products_list",
        parameters=[ProductListQuerySerializer, EXPORT_PARAM],
        responses={200: ProductSerializer(many=True), **ADMIN_ERRORS},
    )
    def list(self, request: Request, *args: Any, **kwargs: Any) -> Response:
        return super().list(request, *args, **kwargs)

    @extend_schema(
        operation_id="admin_products_create",
        request=ProductWriteSerializer,
        responses={201: ProductSerializer, **ADMIN_ERRORS},
    )
    @idempotent(required=False)
    def create(self, request: Request) -> Response:
        services.require_live_billz()  # before the serializer's Billz lookup (customer contract §6.14)
        serializer = ProductWriteSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        product = services.create_product(serializer.to_input(), actor=request.user, request=request)
        return Response(ProductSerializer(product).data, status=201)

    @extend_schema(operation_id="admin_products_retrieve", responses={200: ProductSerializer, **ADMIN_ERRORS})
    def retrieve(self, request: Request, pk: int) -> Response:
        product = selectors.get_product(pk)
        if product is None:
            raise NotFound
        return Response(ProductSerializer(product).data)

    @extend_schema(
        operation_id="admin_products_partial_update",
        request=ProductPatchSerializer,
        responses={200: ProductSerializer, **ADMIN_ERRORS},
    )
    @idempotent(required=False)
    def partial_update(self, request: Request, pk: int) -> Response:
        product = Product.objects.filter(pk=pk).first()
        if product is None:
            raise NotFound
        data = request.data
        submitted = data.get("billz_id") if isinstance(data, dict) else None
        # Compare what the serializer would store (a trimmed string); null, booleans and other types fall
        # through to its 400 without a Billz lookup.
        if (
            isinstance(submitted, (str, int))
            and not isinstance(submitted, bool)
            and str(submitted).strip() != product.billz_id
        ):
            services.require_live_billz()  # before the serializer's Billz lookup (customer contract §6.14)
        serializer = ProductPatchSerializer(instance=product, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        updated = services.update_product(
            product.pk, serializer.to_input(), actor=request.user, request=request
        )
        return Response(ProductSerializer(updated).data)


class ProductStatsView(AdminAPIView):
    @extend_schema(
        operation_id="admin_products_stats", responses={200: ProductStatsSerializer, **ADMIN_ERRORS}
    )
    def get(self, request: Request) -> Response:
        stats: dict[str, Any] = selectors.product_counts()
        stats["billz"] = integration_selectors.billz_status(configured=billz.get_port().is_configured())
        return Response(ProductStatsSerializer(stats).data)


def query_params(request: Request) -> dict[str, str]:
    # A plain dict, not the QueryDict: DRF reads a missing BooleanField from HTML-style input as ``False``,
    # which would turn an absent ``promo`` / ``is_active`` filter into ``?promo=false``.
    return request.query_params.dict()


def _product_or_404(pk: int) -> Product:
    product = Product.objects.filter(pk=pk).first()
    if product is None:
        raise NotFound
    return product


class ProductImagesView(AdminAPIView):
    parser_classes = [MultiPartParser]

    @extend_schema(
        operation_id="admin_product_images_create",
        description="One multipart part `file` (JPEG, PNG or WebP, ≤ 5 MiB, ≤ 8 images per product).",
        request={
            "multipart/form-data": inline_serializer(
                name="ProductImageUpload", fields={"file": serializers.FileField()}
            )
        },
        responses={201: ProductImageSerializer, **ADMIN_ERRORS},
    )
    @idempotent(required=False)
    def post(self, request: Request, pk: int) -> Response:
        product = _product_or_404(pk)
        uploaded = request.FILES.get("file")
        if uploaded is None:
            raise ValidationError({"file": [field_error("required")]})
        image = services.add_image(product.pk, uploaded, actor=request.user, request=request)
        return Response(ProductImageSerializer(image).data, status=201)


class ProductImageDetailView(AdminAPIView):
    @extend_schema(operation_id="admin_product_images_destroy", responses={204: None, **ADMIN_ERRORS})
    @idempotent(required=False)
    def delete(self, request: Request, pk: int, image_id: int) -> Response:
        services.delete_image(pk, image_id, actor=request.user, request=request)
        return Response(status=204)


class ProductImagesOrderView(AdminAPIView):
    @extend_schema(
        operation_id="admin_product_images_reorder",
        request=ImageOrderSerializer,
        responses={200: ImagesResponseSerializer, **ADMIN_ERRORS},
    )
    @idempotent(required=False)
    def put(self, request: Request, pk: int) -> Response:
        product = _product_or_404(pk)
        serializer = ImageOrderSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        images = services.reorder_images(
            product.pk, serializer.validated_data["image_ids"], actor=request.user, request=request
        )
        return Response(ImagesResponseSerializer({"images": images}).data)


# --- categories ---------------------------------------------------------------------------------------------


class CategoryViewSet(ExportMixin, mixins.ListModelMixin, AdminGenericViewSet):
    serializer_class = CategorySerializer
    export_name = "kategoriyalar"
    export_filter_keys = frozenset(CategoryListQuerySerializer().fields)
    export_resource = "categories"
    export_headers = labels.CATEGORY_EXPORT_HEADERS
    lookup_field = "slug"

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
            )

    def _category(self, slug: str) -> Category:
        if slug in RESERVED_SLUGS:
            raise NotFound
        category = selectors.category_queryset().filter(slug=slug).first()
        if category is None:
            raise NotFound
        return category

    @extend_schema(
        operation_id="admin_categories_list",
        parameters=[CategoryListQuerySerializer, EXPORT_PARAM],
        responses={200: CategorySerializer(many=True), **ADMIN_ERRORS},
    )
    def list(self, request: Request, *args: Any, **kwargs: Any) -> Response:
        return super().list(request, *args, **kwargs)

    @extend_schema(
        operation_id="admin_categories_create",
        request=CategoryWriteSerializer,
        responses={201: CategorySerializer, **ADMIN_ERRORS},
    )
    @idempotent(required=False)
    def create(self, request: Request) -> Response:
        serializer = CategoryWriteSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        category = services.create_category(
            data["slug"], data["name"], is_active=data["is_active"], actor=request.user, request=request
        )
        return Response(CategorySerializer(category).data, status=201)

    @extend_schema(
        operation_id="admin_categories_retrieve", responses={200: CategorySerializer, **ADMIN_ERRORS}
    )
    def retrieve(self, request: Request, slug: str) -> Response:
        return Response(CategorySerializer(self._category(slug)).data)

    @extend_schema(
        operation_id="admin_categories_partial_update",
        request=CategoryWriteSerializer,
        responses={200: CategorySerializer, **ADMIN_ERRORS},
    )
    @idempotent(required=False)
    def partial_update(self, request: Request, slug: str) -> Response:
        category = self._category(slug)
        serializer = CategoryWriteSerializer(instance=category, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        updated = services.update_category(
            category,
            name=data.get("name"),
            is_active=data.get("is_active"),
            actor=request.user,
            request=request,
        )
        return Response(CategorySerializer(updated).data)

    @extend_schema(operation_id="admin_categories_destroy", responses={204: None, **ADMIN_ERRORS})
    @idempotent(required=False)
    def destroy(self, request: Request, slug: str) -> Response:
        services.delete_category(self._category(slug), actor=request.user, request=request)
        return Response(status=204)

    @extend_schema(
        operation_id="admin_categories_reorder",
        request=SlugOrderSerializer,
        responses={200: CategoryResultsSerializer, **ADMIN_ERRORS},
    )
    @idempotent(required=False)
    def reorder(self, request: Request) -> Response:
        serializer = SlugOrderSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        categories = services.reorder_categories(
            serializer.validated_data["slugs"], actor=request.user, request=request
        )
        return Response(CategoryResultsSerializer({"results": categories}).data)


# --- banners ------------------------------------------------------------------------------------------------

# Multipart keys whose empty string means ``null`` (see BannerWriteSerializer's docstring).
NULLABLE_FORM_KEYS = frozenset({"link.product_id", "link.category", "starts_at", "ends_at"})


def form_to_nested(data: Any) -> dict[str, Any]:
    """Un-dot multipart keys (``title.uz`` → ``{"title": {"uz": …}}``); JSON bodies pass through."""
    if not isinstance(data, QueryDict):
        return dict(data) if isinstance(data, dict) else {}
    nested: dict[str, Any] = {}
    for key in data:
        # DRF merges request.FILES into request.data, so a value may be a file although the QueryDict stubs
        # only know strings: widen the type so the guard is not flagged as unreachable.
        value: object = data.get(key)
        if isinstance(value, UploadedFile):
            continue
        if key in NULLABLE_FORM_KEYS and value == "":
            value = None
        target = nested
        parts = key.split(".")
        for part in parts[:-1]:
            child = target.get(part)
            if not isinstance(child, dict):
                child = {}
                target[part] = child
            target = child
        target[parts[-1]] = value
    return nested


BannerFormSerializer = inline_serializer(
    name="BannerForm",
    fields={
        "image": serializers.FileField(required=False),
        "title.uz": serializers.CharField(max_length=80),
        "title.ru": serializers.CharField(max_length=80, required=False, allow_blank=True),
        "title.en": serializers.CharField(max_length=80, required=False, allow_blank=True),
        "link.kind": serializers.ChoiceField(choices=enums.BannerLinkKind.values, required=False),
        "link.product_id": serializers.IntegerField(required=False, allow_null=True),
        "link.category": serializers.CharField(required=False, allow_null=True, max_length=40),
        "is_active": serializers.BooleanField(required=False),
        "starts_at": serializers.DateTimeField(required=False, allow_null=True),
        "ends_at": serializers.DateTimeField(required=False, allow_null=True),
    },
)


BannerPatchFormSerializer = inline_serializer(
    name="BannerPatchForm",
    fields={
        "image": serializers.FileField(required=False),
        "title.uz": serializers.CharField(max_length=80, required=False),
        "title.ru": serializers.CharField(max_length=80, required=False, allow_blank=True),
        "title.en": serializers.CharField(max_length=80, required=False, allow_blank=True),
        "link.kind": serializers.ChoiceField(choices=enums.BannerLinkKind.values, required=False),
        "link.product_id": serializers.IntegerField(required=False, allow_null=True),
        "link.category": serializers.CharField(required=False, allow_null=True, max_length=40),
        "is_active": serializers.BooleanField(required=False),
        "starts_at": serializers.DateTimeField(required=False, allow_null=True),
        "ends_at": serializers.DateTimeField(required=False, allow_null=True),
    },
)


class BannerViewSet(ExportMixin, mixins.ListModelMixin, AdminGenericViewSet):
    serializer_class = BannerSerializer
    export_name = "bannerlar"
    export_filter_keys = frozenset(BannerListQuerySerializer().fields)
    export_resource = "banners"
    export_headers = labels.BANNER_EXPORT_HEADERS

    def initialize_request(self, request: HttpRequest, *args: Any, **kwargs: Any) -> Request:
        # DRF picks the parsers before the viewset resolves ``self.action``: resolve it first.
        action_map: dict[str, str] = getattr(self, "action_map", None) or {}
        self.action = action_map.get(request.method.lower() if request.method else "") or ""
        return super().initialize_request(request, *args, **kwargs)

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
                link = f"mahsulot #{banner.link_product_id}"
            elif banner.link_kind == enums.BannerLinkKind.CATEGORY and banner.link_category_id:
                link = f"kategoriya {banner.link_category.slug if banner.link_category else ''}"
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

    @extend_schema(
        operation_id="admin_banners_list",
        parameters=[BannerListQuerySerializer, EXPORT_PARAM],
        responses={200: BannerSerializer(many=True), **ADMIN_ERRORS},
    )
    def list(self, request: Request, *args: Any, **kwargs: Any) -> Response:
        return super().list(request, *args, **kwargs)

    @extend_schema(
        operation_id="admin_banners_create",
        description="Multipart: `image` plus dotted keys `title.uz`, `link.kind`, … (BannerWriteSerializer).",
        request={"multipart/form-data": BannerFormSerializer},
        responses={201: BannerSerializer, **ADMIN_ERRORS},
    )
    @idempotent(required=False)
    def create(self, request: Request) -> Response:
        serializer = BannerWriteSerializer(data=form_to_nested(request.data))
        serializer.is_valid(raise_exception=True)
        image = request.FILES.get("image")
        if image is None:
            raise ValidationError({"image": [field_error("required")]})
        banner = services.create_banner(serializer.to_input(), image, actor=request.user, request=request)
        return Response(BannerSerializer(banner).data, status=201)

    @extend_schema(operation_id="admin_banners_retrieve", responses={200: BannerSerializer, **ADMIN_ERRORS})
    def retrieve(self, request: Request, pk: int) -> Response:
        return Response(BannerSerializer(self._banner(pk)).data)

    @extend_schema(
        operation_id="admin_banners_partial_update",
        request={"multipart/form-data": BannerPatchFormSerializer, "application/json": BannerPatchSerializer},
        responses={200: BannerSerializer, **ADMIN_ERRORS},
    )
    @idempotent(required=False)
    def partial_update(self, request: Request, pk: int) -> Response:
        banner = self._banner(pk)
        serializer = BannerPatchSerializer(instance=banner, data=form_to_nested(request.data), partial=True)
        serializer.is_valid(raise_exception=True)
        updated = services.update_banner(
            banner, serializer.to_input(), request.FILES.get("image"), actor=request.user, request=request
        )
        return Response(BannerSerializer(updated).data)

    @extend_schema(operation_id="admin_banners_destroy", responses={204: None, **ADMIN_ERRORS})
    @idempotent(required=False)
    def destroy(self, request: Request, pk: int) -> Response:
        services.delete_banner(self._banner(pk), actor=request.user, request=request)
        return Response(status=204)

    @extend_schema(
        operation_id="admin_banners_reorder",
        request=IdOrderSerializer,
        responses={200: BannerResultsSerializer, **ADMIN_ERRORS},
    )
    @idempotent(required=False)
    def reorder(self, request: Request) -> Response:
        serializer = IdOrderSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        banners = services.reorder_banners(
            serializer.validated_data["ids"], actor=request.user, request=request
        )
        return Response(BannerResultsSerializer({"results": banners}).data)


# --- Billz import and jobs (contract §2.3 last block, §2.14) ------------------------------------------------
# The views live here, not in ``integrations``: the admin view base (``accounts.api.base``) sits above
# ``integrations`` in the layer contract. The routes stay ``integrations/billz/import/`` and ``jobs/{id}/``.


class BillzImportView(AdminAPIView):
    @extend_schema(
        operation_id="admin_billz_import_create",
        description=(
            "Start a Billz import job (single-flight: a queued or running `billz_import` job is returned "
            "instead of a new one). Poll `jobs/{id}/`."
        ),
        request=BillzImportSerializer,
        responses={202: JobSerializer, **ADMIN_ERRORS},
    )
    @idempotent(required=False)
    def post(self, request: Request) -> Response:
        services.require_live_billz()  # no job while Billz is fake under the production settings
        serializer = BillzImportSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        job, _created = job_services.start_job(
            enums.JobKind.BILLZ_IMPORT,
            {"mode": serializer.validated_data["mode"]},
            actor=request.user,
            request=request,
        )
        return Response(JobSerializer(job).data, status=202)


class JobDetailView(AdminAPIView):
    @extend_schema(operation_id="admin_jobs_retrieve", responses={200: JobSerializer, **ADMIN_ERRORS})
    def get(self, request: Request, pk: int) -> Response:
        job = integration_selectors.get_job(pk)
        if job is None:
            raise NotFound
        return Response(JobSerializer(job).data)
