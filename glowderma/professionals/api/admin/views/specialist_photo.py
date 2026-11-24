"""A specialist's photo: upload and removal."""

from __future__ import annotations

from rest_framework.exceptions import ValidationError
from rest_framework.parsers import MultiPartParser
from rest_framework.request import Request
from rest_framework.response import Response

from glowderma.accounts.api.base import AdminAPIView
from glowderma.core.api.exceptions import field_error
from glowderma.core.idempotency import idempotent
from glowderma.professionals import services
from glowderma.professionals.api.admin.payloads import specialist_body
from glowderma.professionals.api.admin.schemas.specialist_photo import (
    SPECIALIST_PHOTO_DELETE,
    SPECIALIST_PHOTO_PUT,
)
from glowderma.professionals.api.admin.views.common import professional_or_404


class SpecialistPhotoView(AdminAPIView):
    parser_classes = [MultiPartParser]

    @SPECIALIST_PHOTO_PUT
    @idempotent(required=False)
    def put(self, request: Request, pk: int) -> Response:
        professional_or_404(pk)
        upload = request.FILES.get("file")
        if upload is None:
            raise ValidationError({"file": [field_error("required")]})
        services.set_specialist_photo(pk, upload, actor=request.user, request=request)
        return Response(specialist_body(pk))

    @SPECIALIST_PHOTO_DELETE
    @idempotent(required=False)
    def delete(self, request: Request, pk: int) -> Response:
        professional_or_404(pk)
        services.delete_specialist_photo(pk, actor=request.user, request=request)
        return Response(status=204)


__all__ = [
    "SpecialistPhotoView",
]
