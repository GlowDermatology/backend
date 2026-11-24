"""Staff social sign-in and linking (``auth/oauth/{provider}/``)."""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from typing import Any, cast

from django.core.cache import cache
from django.http import HttpRequest
from rest_framework.authentication import BaseAuthentication
from rest_framework.exceptions import NotFound
from rest_framework.permissions import AllowAny, BasePermission
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.throttling import BaseThrottle

from glowderma.accounts import mfa, services
from glowderma.accounts.api.admin.payloads import access_payload, me_payload, mfa_challenge_payload
from glowderma.accounts.api.admin.schemas.social import (
    O_AUTH_LINK_POST,
    O_AUTH_PROVIDER_DELETE,
    O_AUTH_PROVIDER_POST,
)
from glowderma.accounts.api.admin.serializers import (
    OAuthCallbackSerializer,
)
from glowderma.accounts.api.base import WorkforceAPIView
from glowderma.accounts.api.throttles import (
    OAuthIPThrottle,
)
from glowderma.accounts.models import StaffUser
from glowderma.core.api.exceptions import AuthFailed, OAuthExchangeFailed, ProviderUnavailable
from glowderma.integrations.oauth.ports import OAuthExchangeError, SocialClaims
from glowderma.integrations.oauth.registry import PROVIDER_NAMES, get_oauth_provider

# A PKCE verifier is single use across every API instance (admin contract §2.1): the key outlives the
# provider's code (10 min) plus a minute of clock skew.
VERIFIER_TTL_SECONDS = 660


def claim_code_verifier(code_verifier: str) -> bool:
    """Atomically mark ``code_verifier`` used (``cache.add`` on its SHA-256, shared Redis): ``False`` when
    another request (any worker, any instance) already presented it."""
    digest = hashlib.sha256(code_verifier.encode("utf-8")).hexdigest()
    return bool(cache.add(f"oauth:verifier:{digest}", 1, VERIFIER_TTL_SECONDS))


def _exchange(request: Request, provider: str) -> SocialClaims:
    """Validate the callback body and turn the code into verified claims (provider errors -> API errors).

    A reused ``code_verifier`` is refused with the uniform 401 ``social_account_not_allowed`` before the
    provider is called, so a replayed callback (a second tab, a leaked request) cannot sign in twice even when
    the provider would still accept the code.
    """
    oauth = get_oauth_provider(provider)  # 404 for an unknown provider, before the body is read
    serializer = OAuthCallbackSerializer(data=request.data, context={"provider": provider})
    serializer.is_valid(raise_exception=True)
    data = serializer.validated_data
    if not claim_code_verifier(data["code_verifier"]):
        raise AuthFailed("social_account_not_allowed")
    try:
        return oauth.exchange(
            code=data["code"],
            code_verifier=data["code_verifier"],
            redirect_uri=data["redirect_uri"],
            nonce=data.get("nonce"),
        )
    except OAuthExchangeError as exc:
        if exc.kind == "unavailable":
            raise ProviderUnavailable from exc
        raise OAuthExchangeFailed from exc


class OAuthProviderView(WorkforceAPIView):
    """``auth/oauth/{provider}/``: POST signs in without a bearer (public, per-IP throttle), DELETE unlinks
    the caller's identity and is an ordinary admin call. The two methods share one route, so authentication,
    permission and throttling are chosen per request method."""

    public = False  # set per request in initialize_request; False also covers schema generation

    def initialize_request(self, request: HttpRequest, *args: Any, **kwargs: Any) -> Request:
        self.public = request.method == "POST"
        return super().initialize_request(request, *args, **kwargs)

    def get_authenticators(self) -> list[BaseAuthentication]:
        return [] if self.public else super().get_authenticators()

    def get_permissions(self) -> Sequence[BasePermission]:
        return [AllowAny()] if self.public else cast(Sequence[BasePermission], super().get_permissions())

    def get_throttles(self) -> list[BaseThrottle]:
        return [OAuthIPThrottle()] if self.public else super().get_throttles()

    @O_AUTH_PROVIDER_POST
    def post(self, request: Request, provider: str) -> Response:
        claims = _exchange(request, provider)
        try:
            session, refresh = services.social_login(
                provider, claims, device_label=request.META.get("HTTP_USER_AGENT", "")[:100], request=request
            )
        except mfa.MfaRequired as required:
            return Response(mfa_challenge_payload(required.challenge))
        user = cast(StaffUser, session.staff)
        return Response({**access_payload(session), "refresh": refresh, "user": me_payload(user, session)})

    @O_AUTH_PROVIDER_DELETE
    def delete(self, request: Request, provider: str) -> Response:
        if provider not in PROVIDER_NAMES:
            raise NotFound
        services.unlink_identity(cast(StaffUser, request.user), provider, request=request)
        return Response(status=204)


class OAuthLinkView(WorkforceAPIView):
    @O_AUTH_LINK_POST
    def post(self, request: Request, provider: str) -> Response:
        get_oauth_provider(provider)  # 404 first (unknown provider, fake OAuth under the production settings)
        sid = cast(dict[str, Any], request.auth)["sid"]
        session = services.require_password_session(sid)  # 403 before the provider is called
        claims = _exchange(request, provider)
        user = cast(StaffUser, request.user)
        services.link_identity(user, provider, claims, session_id=sid, request=request)
        return Response(me_payload(user, session))


__all__ = [
    "VERIFIER_TTL_SECONDS",
    "OAuthLinkView",
    "OAuthProviderView",
    "claim_code_verifier",
]
