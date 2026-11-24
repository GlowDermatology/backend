"""``me/social/*`` of the customer API (customer contract §2.2.8): linking and unlinking a social identity."""

from __future__ import annotations

from rest_framework.exceptions import NotFound
from rest_framework.request import Request
from rest_framework.response import Response

from glowderma.core.idempotency import idempotent
from glowderma.customers import services
from glowderma.customers.api.customer import serializers as s
from glowderma.customers.api.customer.schemas.delete_body import DeleteBodyAutoSchema
from glowderma.customers.api.customer.schemas.me_social import SOCIAL_LINK_POST, SOCIAL_UNLINK_DELETE
from glowderma.customers.api.customer.views.common import MeBase, require_social_available
from glowderma.integrations.oauth.registry import CUSTOMER_PROVIDERS


class SocialLinkView(MeBase):
    @SOCIAL_LINK_POST
    def post(self, request: Request) -> Response:
        # The Idempotency-Key transaction rolls back with a refusal: an Apple token exchanged for it is
        # revoked after that transaction ended, so the revocation is not rolled back with it.
        with services.revoke_refused_apple_tokens():
            return self.link(request)

    @idempotent()
    def link(self, request: Request) -> Response:
        require_social_available(request.data.get("provider") if isinstance(request.data, dict) else None)
        serializer = s.MeSocialLinkSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        services.link_social(
            self.customer.pk,
            data["provider"],
            data["token"],
            data.get("token_type"),
            data.get("nonce"),
            data.get("authorization_code"),
            password=data.get("password"),
            verification_token=data.get("verification_token"),
            fid=self.fid,
            sid=self.sid,
            request=request,
        )
        return Response(self.me())


class SocialUnlinkView(MeBase):
    schema = DeleteBodyAutoSchema()  # DELETE carries the re-authentication body

    @SOCIAL_UNLINK_DELETE
    @idempotent(required=False)
    def delete(self, request: Request, provider: str) -> Response:
        if provider not in CUSTOMER_PROVIDERS:
            raise NotFound
        serializer = s.ReauthSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        services.unlink_social(
            self.customer.pk,
            provider,
            password=data.get("password"),
            verification_token=data.get("verification_token"),
            sid=self.sid,
            request=request,
        )
        return Response(self.me())


__all__ = [
    "SocialLinkView",
    "SocialUnlinkView",
]
