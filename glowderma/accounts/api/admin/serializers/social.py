"""Staff social sign-in (``auth/oauth/{provider}/``): the provider callback."""

from __future__ import annotations

from typing import Any

from django.conf import settings
from rest_framework import serializers

from glowderma.core.api.exceptions import field_error


def redirect_uri_for(provider: str) -> str:
    """The configured Next callback of ``provider`` (admin contract §2.1), ``""`` when not configured.

    ``GOOGLE_OAUTH_REDIRECT_URI`` / ``FACEBOOK_OAUTH_REDIRECT_URI``; the settings module already falls back to
    the deprecated single ``OAUTH_REDIRECT_URI`` (one release), and so does an override of only that value.
    """
    specific = {
        "google": getattr(settings, "GOOGLE_OAUTH_REDIRECT_URI", ""),
        "facebook": getattr(settings, "FACEBOOK_OAUTH_REDIRECT_URI", ""),
    }.get(provider, "")
    return str(specific or getattr(settings, "OAUTH_REDIRECT_URI", ""))


class OAuthCallbackSerializer(serializers.Serializer[dict[str, Any]]):
    """Body of ``POST auth/oauth/{provider}/`` and ``.../link/`` (contract 2.1).

    ``context["provider"]`` decides whether ``nonce`` is required (Google only). ``redirect_uri`` must equal
    the provider's configured callback (``GOOGLE_OAUTH_REDIRECT_URI`` / ``FACEBOOK_OAUTH_REDIRECT_URI``, which
    fall back to the deprecated ``OAUTH_REDIRECT_URI``) when that is set.
    """

    code = serializers.CharField(min_length=1, max_length=2048, trim_whitespace=False)
    code_verifier = serializers.CharField(min_length=43, max_length=128, trim_whitespace=False)
    redirect_uri = serializers.URLField(max_length=2048)
    nonce = serializers.CharField(max_length=512, required=False, trim_whitespace=False)

    def validate_redirect_uri(self, value: str) -> str:
        configured = redirect_uri_for(str(self.context.get("provider", "")))
        if configured and value != configured:
            raise serializers.ValidationError([field_error("invalid")])
        return value

    def validate(self, attrs: dict[str, Any]) -> dict[str, Any]:
        if self.context.get("provider") == "google" and "nonce" not in attrs:
            raise serializers.ValidationError({"nonce": [field_error("required")]})
        return attrs


__all__ = [
    "OAuthCallbackSerializer",
    "redirect_uri_for",
]
