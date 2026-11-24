"""``auth/*`` shapes (customer contract §2.1): the identifier, OTP, registration, login, reset, refresh and
logout bodies and their answers."""

from __future__ import annotations

from typing import Any

from rest_framework import serializers

from glowderma.core import enums
from glowderma.core.api.exceptions import field_error
from glowderma.customers.api.customer.serializers.fields import (
    CODE_PATTERN,
    CustomerEmailField,
    CustomerPhoneField,
    clean_name,
    code_field,
    name_field,
    password_field,
)
from glowderma.customers.api.customer.serializers.me import CustomerMeSerializer


class IdentifierSerializer(serializers.Serializer[dict[str, Any]]):
    """``phone`` **or** ``email`` (hybrid identity, customer contract §2.1): exactly one per call. Neither →
    ``phone`` ``required`` (``field.identifier_required``); both → ``email`` ``invalid``
    (``field.identifier_ambiguous``). ``validated_data["identifier"]`` is the one given (an address holds
    ``@``, a phone never does)."""

    phone = CustomerPhoneField(required=False, allow_null=True)
    email = CustomerEmailField(required=False, allow_null=True)

    def validate(self, attrs: dict[str, Any]) -> dict[str, Any]:
        phone, email = attrs.get("phone"), attrs.get("email")
        if phone and email:
            raise serializers.ValidationError(
                {"email": [field_error("invalid", message_key="field.identifier_ambiguous")]}
            )
        if not phone and not email:
            raise serializers.ValidationError(
                {"phone": [field_error("required", message_key="field.identifier_required")]}
            )
        attrs["identifier"] = phone or email
        return attrs


# Purposes ``auth/send-code/`` and ``auth/verify-code/`` refuse (``invalid_choice``): the new-device code is
# issued only by ``auth/login/``; ``attach_phone`` / ``verify_email`` belong to ``me/phone/`` / ``me/email/``.
SIGNED_IN_PURPOSES = frozenset({enums.OtpPurpose.ATTACH_PHONE, enums.OtpPurpose.VERIFY_EMAIL})


class SendCodeSerializer(IdentifierSerializer):
    purpose = serializers.ChoiceField(choices=enums.OtpPurpose.values)
    social_ticket = serializers.CharField(required=False, allow_null=True, allow_blank=True, max_length=2048)

    def validate_purpose(self, value: str) -> str:
        # login_new_device: issued only by auth/login/ after the password
        if value == enums.OtpPurpose.LOGIN_NEW_DEVICE or value in SIGNED_IN_PURPOSES:
            raise serializers.ValidationError([field_error("invalid_choice")])
        return value

    def validate(self, attrs: dict[str, Any]) -> dict[str, Any]:
        attrs = super().validate(attrs)
        if attrs["purpose"] == enums.OtpPurpose.LINK_SOCIAL and not attrs.get("social_ticket"):
            raise serializers.ValidationError(
                {"social_ticket": [field_error("required", message_key="field.social_ticket_required")]}
            )
        return attrs


class VerifyCodeSerializer(IdentifierSerializer):
    purpose = serializers.ChoiceField(choices=enums.OtpPurpose.values)
    code = code_field()

    def validate_purpose(self, value: str) -> str:
        if value in SIGNED_IN_PURPOSES:  # verified by me/phone/ and me/email/ themselves
            raise serializers.ValidationError([field_error("invalid_choice")])
        return value

    def validate_code(self, value: str) -> str:
        if not CODE_PATTERN.fullmatch(value):
            raise serializers.ValidationError([field_error("invalid", message_key="field.otp_format")])
        return value


class SetPasswordSerializer(serializers.Serializer[dict[str, Any]]):
    verification_token = serializers.CharField(max_length=128)
    password = password_field()
    name = name_field()
    terms_version = serializers.CharField(max_length=32)
    language = serializers.ChoiceField(choices=enums.ContentLanguage.values, required=False, allow_null=True)

    def validate_name(self, value: str) -> str | None:
        return clean_name(value)


class CustomerLoginSerializer(IdentifierSerializer):
    password = password_field()
    verification_token = serializers.CharField(
        max_length=128, required=False, allow_null=True, allow_blank=True
    )


class ResetPasswordSerializer(serializers.Serializer[dict[str, Any]]):
    verification_token = serializers.CharField(max_length=128)
    password = password_field()


class CustomerRefreshSerializer(serializers.Serializer[dict[str, Any]]):
    refresh = serializers.CharField(max_length=256, trim_whitespace=False)


class CustomerLogoutSerializer(CustomerRefreshSerializer):
    """``auth/logout/`` (B-W1): ``current`` = this session, ``others`` = every other session, ``all`` =
    everywhere."""

    scope = serializers.ChoiceField(choices=enums.LogoutScope.values, default=enums.LogoutScope.CURRENT)


class AuthTokensSerializer(serializers.Serializer[dict[str, Any]]):
    access = serializers.CharField()
    access_expires_at = serializers.DateTimeField()
    refresh = serializers.CharField()
    refresh_expires_at = serializers.DateTimeField()
    device_key = serializers.CharField(allow_null=True)
    customer = CustomerMeSerializer()
    is_new_customer = serializers.BooleanField()
    reactivated = serializers.BooleanField()


class OtpInfoSerializer(serializers.Serializer[dict[str, Any]]):
    expires_in = serializers.IntegerField()
    resend_in = serializers.IntegerField()
    length = serializers.IntegerField()


class NextStepSerializer(serializers.Serializer[dict[str, Any]]):
    next_step = serializers.ChoiceField(choices=enums.NextStep.values)
    phone_mask = serializers.CharField(allow_null=True)
    otp = OtpInfoSerializer(allow_null=True)
    social_ticket = serializers.CharField(allow_null=True)
    provider = serializers.ChoiceField(choices=enums.CustomerSocialProvider.values, allow_null=True)
    email_mask = serializers.CharField(allow_null=True)
    expires_in = serializers.IntegerField()


class CodeIssueSerializer(serializers.Serializer[dict[str, Any]]):
    phone_mask = serializers.CharField(allow_null=True, help_text="The code went by SMS; else null.")
    email_mask = serializers.CharField(allow_null=True, help_text="The code went by e-mail; else null.")
    purpose = serializers.ChoiceField(choices=enums.OtpPurpose.values)
    expires_in = serializers.IntegerField()
    resend_in = serializers.IntegerField()
    attempts = serializers.IntegerField()
    length = serializers.IntegerField()


class VerificationSerializer(serializers.Serializer[dict[str, Any]]):
    verification_token = serializers.CharField()
    expires_in = serializers.IntegerField()
    purpose = serializers.ChoiceField(choices=enums.OtpPurpose.values)
    customer_exists = serializers.BooleanField()


class RefreshResponseSerializer(serializers.Serializer[dict[str, Any]]):
    access = serializers.CharField()
    access_expires_at = serializers.DateTimeField()
    refresh = serializers.CharField()
    refresh_expires_at = serializers.DateTimeField()


__all__ = [
    "SIGNED_IN_PURPOSES",
    "AuthTokensSerializer",
    "CodeIssueSerializer",
    "CustomerLoginSerializer",
    "CustomerLogoutSerializer",
    "CustomerRefreshSerializer",
    "IdentifierSerializer",
    "NextStepSerializer",
    "OtpInfoSerializer",
    "RefreshResponseSerializer",
    "ResetPasswordSerializer",
    "SendCodeSerializer",
    "SetPasswordSerializer",
    "VerificationSerializer",
    "VerifyCodeSerializer",
]
