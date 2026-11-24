"""The staff account shapes of a specialist: the invite credentials and the role change."""

from __future__ import annotations

from typing import Any

from rest_framework import serializers

from glowderma.accounts.api.admin.serializers import (
    StaffCredentialsSerializer,
    StaffMemberSerializer,
)
from glowderma.core.api.exceptions import field_error


class SpecialistInviteCredentialsSerializer(StaffCredentialsSerializer):
    specialist_id = serializers.IntegerField(help_text="The draft profile created with the account.")


class SpecialistRoleChangeSerializer(serializers.Serializer[dict[str, Any]]):
    role = serializers.ChoiceField(choices=("staff", "specialist"))
    confirmed = serializers.BooleanField(help_text="Must be `true`: the role change is a confirmed action.")

    def validate_confirmed(self, value: bool) -> bool:
        if not value:
            raise serializers.ValidationError(field_error("required"))
        return value


class SpecialistRoleChangeResultSerializer(serializers.Serializer[dict[str, Any]]):
    account = StaffMemberSerializer()
    specialist_id = serializers.IntegerField(
        allow_null=True, help_text="The profile the account owns; `null` for an account that never had one."
    )
    upcoming_appointments = serializers.IntegerField(
        help_text="→ staff: requested/confirmed visits ahead on the paused profile, for staff to handle."
    )


__all__ = [
    "SpecialistInviteCredentialsSerializer",
    "SpecialistRoleChangeResultSerializer",
    "SpecialistRoleChangeSerializer",
]
