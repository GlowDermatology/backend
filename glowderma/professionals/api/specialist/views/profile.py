"""The specialist's own profile and photo."""

from __future__ import annotations

from rest_framework.exceptions import ValidationError
from rest_framework.parsers import MultiPartParser
from rest_framework.request import Request
from rest_framework.response import Response

from glowderma.core.api.exceptions import field_error
from glowderma.core.idempotency import idempotent
from glowderma.professionals import services
from glowderma.professionals.api.specialist.base import SpecialistProfileAPIView
from glowderma.professionals.api.specialist.payloads import own_profile_body
from glowderma.professionals.api.specialist.schemas.profile import (
    OWN_PHOTO_DELETE,
    OWN_PHOTO_PUT,
    OWN_PROFILE_GET,
    OWN_PROFILE_PATCH,
)
from glowderma.professionals.api.specialist.serializers import (
    OwnProfileSerializer,
)


class OwnProfileView(SpecialistProfileAPIView):
    @OWN_PROFILE_GET
    def get(self, request: Request) -> Response:
        return Response(own_profile_body(self.professional.pk))

    @OWN_PROFILE_PATCH
    @idempotent(required=False)
    def patch(self, request: Request) -> Response:
        body = OwnProfileSerializer(
            data=request.data, partial=True, context={"professional_id": self.professional.pk}
        )
        body.is_valid(raise_exception=True)
        data = dict(body.validated_data)
        force = bool(data.pop("force", False))
        services.update_own_profile(
            self.professional.pk, data, force=force, actor=request.user, request=request
        )
        return Response(own_profile_body(self.professional.pk))


class OwnPhotoView(SpecialistProfileAPIView):
    parser_classes = [MultiPartParser]

    @OWN_PHOTO_PUT
    @idempotent(required=False)
    def put(self, request: Request) -> Response:
        upload = request.FILES.get("file")
        if upload is None:
            raise ValidationError({"file": [field_error("required")]})
        services.set_specialist_photo(self.professional.pk, upload, actor=request.user, request=request)
        return Response(own_profile_body(self.professional.pk))

    @OWN_PHOTO_DELETE
    @idempotent(required=False)
    def delete(self, request: Request) -> Response:
        services.delete_specialist_photo(self.professional.pk, actor=request.user, request=request)
        return Response(status=204)


__all__ = [
    "OwnPhotoView",
    "OwnProfileView",
]
