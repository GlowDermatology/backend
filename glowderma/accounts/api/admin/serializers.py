from typing import Any

from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError as DjangoValidationError
from rest_framework import serializers


class LoginSerializer(serializers.Serializer[dict[str, Any]]):
    email = serializers.EmailField(max_length=254)
    password = serializers.CharField(max_length=128, trim_whitespace=False, write_only=True)

    def validate_email(self, value: str) -> str:
        return value.strip().lower()


class RefreshSerializer(serializers.Serializer[dict[str, Any]]):
    refresh = serializers.CharField(trim_whitespace=False)


class LogoutSerializer(RefreshSerializer):
    scope = serializers.ChoiceField(choices=["current", "others", "all"], default="current")


class ChangePasswordSerializer(serializers.Serializer[dict[str, Any]]):
    current_password = serializers.CharField(max_length=128, trim_whitespace=False, write_only=True)
    new_password = serializers.CharField(max_length=128, trim_whitespace=False, write_only=True)

    def validate_new_password(self, value: str) -> str:
        user = self.context["request"].user
        try:
            validate_password(value, user)
        except DjangoValidationError as exc:
            messages = {
                "password_too_short": "Kamida 12 ta belgi.",
                "password_too_common": "Juda oddiy parol.",
                "password_entirely_numeric": "Faqat raqamlardan iborat bo‘lmasin.",
                "password_too_similar": "Login bilan juda o‘xshash.",
            }
            raise serializers.ValidationError(
                [
                    serializers.ErrorDetail(
                        messages.get(error.code or "", str(error.message)), code=error.code or "invalid"
                    )
                    for error in exc.error_list
                ]
            ) from exc
        return value


class MeSerializer(serializers.Serializer[dict[str, Any]]):
    id = serializers.IntegerField()
    email = serializers.EmailField()
    display_name = serializers.CharField()
    is_superuser = serializers.BooleanField()
    linked_providers = serializers.ListField(child=serializers.ChoiceField(choices=["google", "facebook"]))
    session_expires_at = serializers.DateTimeField()


class AccessSerializer(serializers.Serializer[dict[str, Any]]):
    access = serializers.CharField()
    access_expires_at = serializers.DateTimeField()
    refresh_expires_at = serializers.DateTimeField()


class LoginResponseSerializer(AccessSerializer):
    refresh = serializers.CharField()
    user = MeSerializer()
