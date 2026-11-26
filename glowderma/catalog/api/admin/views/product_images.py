"""A product's images: upload, alt texts, removal and order."""

from __future__ import annotations

from rest_framework.exceptions import NotFound, ValidationError
from rest_framework.parsers import MultiPartParser
from rest_framework.request import Request
from rest_framework.response import Response

from glowderma.accounts.api.base import AdminAPIView
from glowderma.catalog import services
from glowderma.catalog.api.admin.schemas.product_images import (
    PRODUCT_IMAGE_DETAIL_DELETE,
    PRODUCT_IMAGE_DETAIL_PATCH,
    PRODUCT_IMAGE_RENDITIONS_PUT,
    PRODUCT_IMAGES_ORDER_PUT,
    PRODUCT_IMAGES_POST,
)
from glowderma.catalog.api.admin.serializers import (
    ImageOrderSerializer,
    ImagePatchSerializer,
    ImageRenditionsPutSerializer,
    ImagesResponseSerializer,
    ProductImageSerializer,
)
from glowderma.catalog.models import Product
from glowderma.core import renditions
from glowderma.core.api.exceptions import field_error
from glowderma.core.idempotency import idempotent


def product_or_404(pk: int) -> Product:
    product = Product.objects.filter(pk=pk).first()
    if product is None:
        raise NotFound
    return product


class ProductImagesView(AdminAPIView):
    parser_classes = [MultiPartParser]

    @PRODUCT_IMAGES_POST
    @idempotent(required=False)
    def post(self, request: Request, pk: int) -> Response:
        product = product_or_404(pk)
        uploaded = request.FILES.get("file")
        if uploaded is None:
            raise ValidationError({"file": [field_error("required")]})
        image = services.add_image(product.pk, uploaded, actor=request.user, request=request)
        return Response(ProductImageSerializer(image).data, status=201)


class ProductImageDetailView(AdminAPIView):
    @PRODUCT_IMAGE_DETAIL_PATCH
    @idempotent(required=False)
    def patch(self, request: Request, pk: int, image_id: int) -> Response:
        serializer = ImagePatchSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        image = services.update_image(
            pk, image_id, alt=serializer.validated_data["alt"], actor=request.user, request=request
        )
        return Response(ProductImageSerializer(image).data)

    @PRODUCT_IMAGE_DETAIL_DELETE
    @idempotent(required=False)
    def delete(self, request: Request, pk: int, image_id: int) -> Response:
        services.delete_image(pk, image_id, actor=request.user, request=request)
        return Response(status=204)


class ProductImageRenditionsView(AdminAPIView):
    @PRODUCT_IMAGE_RENDITIONS_PUT
    @idempotent(required=False)
    def put(self, request: Request, pk: int, image_id: int) -> Response:
        serializer = ImageRenditionsPutSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        changes: dict[str, object] = {
            renditions.FROM_API[name]: value
            for name, value in serializer.validated_data.items()
            if name in renditions.FROM_API
        }
        image = services.set_image_renditions(pk, image_id, changes, actor=request.user, request=request)
        return Response(ProductImageSerializer(image).data)


class ProductImagesOrderView(AdminAPIView):
    @PRODUCT_IMAGES_ORDER_PUT
    @idempotent(required=False)
    def put(self, request: Request, pk: int) -> Response:
        product = product_or_404(pk)
        serializer = ImageOrderSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        images = services.reorder_images(
            product.pk, serializer.validated_data["image_ids"], actor=request.user, request=request
        )
        return Response(ImagesResponseSerializer({"images": images}).data)


__all__ = [
    "ProductImageDetailView",
    "ProductImageRenditionsView",
    "ProductImagesOrderView",
    "ProductImagesView",
    "product_or_404",
]
