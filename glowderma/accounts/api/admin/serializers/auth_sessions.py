"""Sign-in sessions of the admin API: login, refresh, logout and the access-token answers."""

from __future__ import annotations

from typing import Any

from rest_framework import serializers

from glowderma.accounts.api.admin.serializers.profile import MeSerializer


class LoginSerializer(serializers.Serializer[dict[str, Any]]):
    email = serializers.EmailField(max_length=254)
    password = serializers.CharField(max_length=128, trim_whitespace=False, write_only=True)

    def validate_email(self, value: str) -> str:
        return value.strip().lower()


class RefreshSerializer(serializers.Serializer[dict[str, Any]]):
    refresh = serializers.CharField(trim_whitespace=False)


class LogoutSerializer(RefreshSerializer):
    scope = serializers.ChoiceField(choices=["current", "others", "all"], default="current")


class AccessSerializer(serializers.Serializer[dict[str, Any]]):
    access = serializers.CharField()
    access_expires_at = serializers.DateTimeField()
    refresh_expires_at = serializers.DateTimeField()


class LoginResponseSerializer(AccessSerializer):
    refresh = serializers.CharField()
    user = MeSerializer()


__all__ = [
    "AccessSerializer",
    "LoginResponseSerializer",
    "LoginSerializer",
    "LogoutSerializer",
    "RefreshSerializer",
]
