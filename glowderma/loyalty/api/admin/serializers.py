"""Admin serializers of `loyalty`: the cashback policy (contract §2.7, display rules §5.4).

``PolicyVersion`` renders a ``CashbackPolicy`` row loaded by ``loyalty.selectors.policy_versions()`` (derived
``superseded_by`` / ``effective_to`` annotated, author joined, tiers prefetched); ``state`` is derived at the
``now`` passed in the serializer context, so one request renders every version at the same instant.

The tier key ``from`` is a Python keyword, so the two tier serializers are built with ``type()``; the class
names still give the OpenAPI components their names (``CashbackTier`` / ``CashbackTierInput``).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, cast

from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers

from glowderma.core import enums, timeutils
from glowderma.core.api.exceptions import field_error
from glowderma.core.api.fields import AwareDateTimeField, PercentField
from glowderma.core.api.serializers import ActorSerializer
from glowderma.loyalty.engine import TierBand
from glowderma.loyalty.models import CashbackPolicy

BIGINT_MAX = 9_223_372_036_854_775_807  # CashbackTier.from_amount / to_amount are BigIntegerField

CashbackTierSerializer = cast(
    type[serializers.Serializer[Any]],
    type(
        "CashbackTierSerializer",
        (serializers.Serializer,),
        {
            "__module__": __name__,
            "__doc__": "Contract §2.7 tier: ``from`` inclusive, ``to`` exclusive | null, 2-dp percent.",
            "id": serializers.IntegerField(),
            "from": serializers.IntegerField(source="from_amount"),
            "to": serializers.IntegerField(source="to_amount", allow_null=True),
            "percent": PercentField(),
        },
    ),
)

CashbackTierInputSerializer = cast(
    type[serializers.Serializer[dict[str, Any]]],
    type(
        "CashbackTierInputSerializer",
        (serializers.Serializer,),
        {
            "__module__": __name__,
            "__doc__": (
                "One submitted tier. ``percent`` is a decimal string; its range and precision are the policy "
                "rules (``out_of_range`` / ``max_decimal_places``) checked by ``create_policy_version``."
            ),
            "from": serializers.IntegerField(min_value=0, max_value=BIGINT_MAX),
            "to": serializers.IntegerField(
                allow_null=True, required=False, default=None, max_value=BIGINT_MAX, min_value=-BIGINT_MAX
            ),
            # No digit limits here: the service reports ``out_of_range`` before ``max_decimal_places``.
            "percent": PercentField(max_digits=None, decimal_places=None),
        },
    ),
)


class TierListField(serializers.ListField):
    """``tiers``: a JSON list of tiers; anything else answers the contract code ``invalid``."""

    default_error_messages = {"invalid": field_error("invalid")}

    def to_internal_value(self, data: Any) -> list[Any]:
        if isinstance(data, (str, dict)) or not hasattr(data, "__iter__"):
            self.fail("invalid")
        return super().to_internal_value(data)


class PolicyVersionSerializer(serializers.Serializer[CashbackPolicy]):
    """Contract §2.7 ``PolicyVersion`` (state derived at ``context["now"]``)."""

    version = serializers.IntegerField()
    state = serializers.SerializerMethodField()
    effective_from = serializers.DateTimeField()
    effective_to = serializers.DateTimeField(allow_null=True)
    basis = serializers.ChoiceField(choices=enums.RateBasis.values)
    earn_on_cashback_paid = serializers.BooleanField()
    tiers = serializers.SerializerMethodField()
    author = ActorSerializer(allow_null=True)
    created_at = serializers.DateTimeField()
    superseded_by = serializers.IntegerField(allow_null=True)

    def _now(self) -> datetime:
        now = self.context.get("now")
        return now if isinstance(now, datetime) else timeutils.now()

    @extend_schema_field(serializers.ChoiceField(choices=enums.PolicyVersionState.values))
    def get_state(self, obj: CashbackPolicy) -> str:
        return obj.state_at(self._now())

    @extend_schema_field(CashbackTierSerializer(many=True))
    def get_tiers(self, obj: CashbackPolicy) -> list[dict[str, Any]]:
        tiers = sorted(obj.tiers.all(), key=lambda tier: (tier.from_amount, tier.pk))
        return cast(list[dict[str, Any]], CashbackTierSerializer(tiers, many=True).data)


class CashbackPolicySerializer(serializers.Serializer[dict[str, Any]]):
    """``GET cashback/policy/``: the active and scheduled versions, every version and the suggested start."""

    active = PolicyVersionSerializer(allow_null=True)
    scheduled = PolicyVersionSerializer(allow_null=True)
    versions = PolicyVersionSerializer(many=True)
    next_effective_from = serializers.DateTimeField()


class PolicyVersionCreateSerializer(serializers.Serializer[dict[str, Any]]):
    """``POST cashback/policy-versions/`` body; the §2.7 rules run in ``create_policy_version``."""

    effective_from = AwareDateTimeField()
    basis = serializers.ChoiceField(choices=enums.RateBasis.values)
    earn_on_cashback_paid = serializers.BooleanField()
    tiers = TierListField(child=CashbackTierInputSerializer())

    def tier_bands(self) -> list[TierBand]:
        """The submitted tiers in the submitted order (the service reports errors by submitted index)."""
        return [TierBand(tier["from"], tier["to"], tier["percent"]) for tier in self.validated_data["tiers"]]


TierWarningSerializer = cast(
    type[serializers.Serializer[Any]],
    type(
        "TierWarningSerializer",
        (serializers.Serializer,),
        {
            "__module__": __name__,
            "__doc__": "``warnings[]``: ``gap`` = ``[from, to)`` uncovered; ``uncovered_above``: open top.",
            "code": serializers.ChoiceField(choices=enums.TierWarning.values),
            "from": serializers.IntegerField(source="from_amount"),
            "to": serializers.IntegerField(source="to_amount", allow_null=True),
        },
    ),
)


class PolicyVersionCreatedSerializer(serializers.Serializer[dict[str, Any]]):
    """``POST cashback/policy-versions/`` 201."""

    version = PolicyVersionSerializer()
    superseded_version = serializers.IntegerField(allow_null=True)
    warnings = TierWarningSerializer(many=True)
