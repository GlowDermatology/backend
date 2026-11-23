"""The staff directory (``GET staff/``): the list query, the list and detail rows and the pending invite."""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers

from glowderma.accounts import selectors
from glowderma.core import enums
from glowderma.core.api.fields import QueryBooleanField
from glowderma.core.api.serializers import ActorSerializer

STAFF_SEARCH_MAX_LENGTH = 64


class StaffListQuerySerializer(serializers.Serializer[dict[str, Any]]):
    """``GET staff/``: ``search`` (name or email, trimmed, ≤ 64), ``is_active`` (default ``true``)."""

    search = serializers.CharField(required=False, max_length=STAFF_SEARCH_MAX_LENGTH, allow_blank=True)
    is_active = QueryBooleanField(required=False, default=True)

    def validate(self, attrs: dict[str, Any]) -> dict[str, Any]:
        if not attrs.get("search"):
            attrs.pop("search", None)
        return attrs


LINKED_PROVIDERS = ["google", "facebook"]


# Keys a ``StaffListItem`` fills only for a manager (``null`` for everyone else, §2.16).
MANAGER_ONLY_KEYS = (
    "is_superuser",
    "has_ops_console_access",
    "full_name",
    "is_manager",
    "role",
    "last_login_at",
    "linked_providers",
    "created_at",
    "must_change_password",
    "mfa_enabled",
    "invite",
)


def linked_providers(user: Any) -> list[str]:
    """The providers of the user's prefetched social identities (``selectors.with_member_columns``)."""
    return [identity.provider for identity in user.social_identities.all()]


class StaffInviteSerializer(serializers.Serializer[dict[str, Any]]):
    """``StaffMember.invite``: the newest password link (invite or reset) of the account."""

    state = serializers.ChoiceField(
        choices=enums.StaffInviteState.values,
        help_text="`none` (never e-mailed a link), `pending` (open: queued or sent), `expired`, `used` (the "
        "person set a password with it), `revoked` (replaced by a newer link, a password change, a "
        "temporary-password reset or a deactivation).",
    )
    sent_at = serializers.DateTimeField(
        allow_null=True, help_text="When the e-mail went to the relay; `null` while queued or when not sent."
    )
    expires_at = serializers.DateTimeField(allow_null=True)


def invite_data(user: Any) -> dict[str, Any]:
    return dict(StaffInviteSerializer(asdict(selectors.invite_of(user))).data)


class StaffListItemSerializer(ActorSerializer):
    """One ``GET staff/`` row: the ``Actor`` keys plus ``email`` and ``is_active`` for every caller (the
    audit-log actor filter), and the manager's columns, which are ``null`` unless the caller is a manager
    (``context["manager"]``)."""

    email = serializers.EmailField()
    is_active = serializers.BooleanField()
    full_name = serializers.CharField(allow_null=True, help_text="Manager only, else `null`.")
    is_manager = serializers.BooleanField(allow_null=True, help_text="Manager only, else `null`.")
    is_superuser = serializers.BooleanField(allow_null=True, required=False)
    has_ops_console_access = serializers.BooleanField(source="is_staff", allow_null=True, required=False)
    role = serializers.ChoiceField(choices=enums.StaffRole.values, allow_null=True)
    last_login_at = serializers.DateTimeField(
        allow_null=True, help_text="Manager only, else `null`; `null` for a manager too when never signed in."
    )
    linked_providers = serializers.SerializerMethodField(help_text="Manager only, else `null`.")
    created_at = serializers.DateTimeField(
        source="date_joined", allow_null=True, help_text="Manager only, else `null`."
    )
    must_change_password = serializers.BooleanField(allow_null=True, help_text="Manager only, else `null`.")
    mfa_enabled = serializers.SerializerMethodField(
        help_text="A confirmed admin panel TOTP device (the staff screen's badge and «MFA ni o‘chirish»). "
        "Manager only, else `null`."
    )
    invite = serializers.SerializerMethodField(help_text="Manager only, else `null`.")

    @extend_schema_field(
        serializers.ListField(child=serializers.ChoiceField(choices=LINKED_PROVIDERS), allow_null=True)
    )
    def get_linked_providers(self, obj: Any) -> list[str] | None:
        return linked_providers(obj)

    @extend_schema_field(serializers.BooleanField(allow_null=True))
    def get_mfa_enabled(self, obj: Any) -> bool | None:
        return bool(getattr(obj, "mfa_enabled", False))

    @extend_schema_field(StaffInviteSerializer(allow_null=True))
    def get_invite(self, obj: Any) -> dict[str, Any] | None:
        return invite_data(obj)

    def to_representation(self, instance: Any) -> dict[str, Any]:
        if self.context.get("manager"):
            return dict(super().to_representation(instance))
        # Built by hand so a non-manager's page never reads the manager's columns.
        return {
            "id": instance.pk,
            "display_name": self.get_display_name(instance),
            "email": instance.email,
            "is_active": instance.is_active,
            **dict.fromkeys(MANAGER_ONLY_KEYS),
        }


class StaffMemberSerializer(ActorSerializer):
    """``StaffMember`` (§2.16): a staff account as managers see it; every write of ``staff/`` returns it."""

    email = serializers.EmailField()
    full_name = serializers.CharField(allow_blank=True)
    is_active = serializers.BooleanField()
    is_manager = serializers.BooleanField()
    role = serializers.ChoiceField(choices=enums.StaffRole.values)
    is_superuser = serializers.BooleanField(required=False)
    has_ops_console_access = serializers.BooleanField(source="is_staff", required=False)
    last_login_at = serializers.DateTimeField(allow_null=True, help_text="`null`: never signed in.")
    linked_providers = serializers.SerializerMethodField()
    created_at = serializers.DateTimeField(source="date_joined")
    must_change_password = serializers.BooleanField(
        help_text="The person must set their own password before using the admin panel."
    )
    password_changed_at = serializers.DateTimeField(
        allow_null=True,
        help_text="When the person last set their own password (a link or `auth/change-password/`); `null`: "
        "never since this was recorded.",
    )
    mfa_enabled = serializers.SerializerMethodField(help_text="A confirmed admin panel TOTP device.")
    invite = serializers.SerializerMethodField()

    @extend_schema_field(serializers.ListField(child=serializers.ChoiceField(choices=LINKED_PROVIDERS)))
    def get_linked_providers(self, obj: Any) -> list[str]:
        return linked_providers(obj)

    @extend_schema_field(serializers.BooleanField())
    def get_mfa_enabled(self, obj: Any) -> bool:
        return bool(getattr(obj, "mfa_enabled", False))

    @extend_schema_field(StaffInviteSerializer())
    def get_invite(self, obj: Any) -> dict[str, Any]:
        return invite_data(obj)


__all__ = [
    "LINKED_PROVIDERS",
    "MANAGER_ONLY_KEYS",
    "STAFF_SEARCH_MAX_LENGTH",
    "StaffInviteSerializer",
    "StaffListItemSerializer",
    "StaffListQuerySerializer",
    "StaffMemberSerializer",
    "invite_data",
    "linked_providers",
]
