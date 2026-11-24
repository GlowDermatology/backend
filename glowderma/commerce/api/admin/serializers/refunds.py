"""The body of a refund resolution (contract §7 item 18)."""

from __future__ import annotations

from typing import Any

from rest_framework import serializers

from glowderma.core import enums
from glowderma.core.api.exceptions import field_error

RESOLVE_STATES = (enums.RefundState.SUCCEEDED, enums.RefundState.FAILED)


class ResolveRefundSerializer(serializers.Serializer[dict[str, Any]]):
    """``{state: succeeded | failed, manual_payout?: bool}`` (a ``CharField`` so the schema keeps one
    ``RefundStateEnum``); ``manual_payout`` (admin §7 item 18, M2) closes a ``failed`` Click refund Click
    still reports ``paid`` after staff paid the customer outside Click; ignored for other refunds."""

    state = serializers.CharField(help_text="`succeeded` or `failed`")
    manual_payout = serializers.BooleanField(
        required=False,
        default=False,
        help_text="Staff paid the customer outside Click (closing a `failed` Click refund Click reports "
        "`paid`).",
    )

    def validate_state(self, value: str) -> str:
        if value not in RESOLVE_STATES:
            raise serializers.ValidationError(field_error("invalid_choice"))
        return value


__all__ = [
    "RESOLVE_STATES",
    "ResolveRefundSerializer",
]
