"""Admin views of `loyalty`: ``GET cashback/policy/`` and ``POST cashback/policy-versions/`` (contract §2.7).

Every rule lives in ``loyalty.services`` / ``loyalty.selectors``; the views only shape requests and responses.
``PolicyValidationError`` carries the §2.7 field errors (paths ``tiers.{i}.…`` with the submitted index) and
the table's top-level message, raised here as a ``ContractValidationError``.
"""

from __future__ import annotations

from typing import Any, cast

from rest_framework.request import Request
from rest_framework.response import Response

from glowderma.accounts.api.base import AdminAPIView
from glowderma.core import enums, timeutils
from glowderma.core.api.exceptions import ContractValidationError
from glowderma.core.idempotency import idempotent
from glowderma.loyalty import selectors, services
from glowderma.loyalty.api.admin.schemas import CASHBACK_POLICY_GET, POLICY_VERSION_CREATE_POST
from glowderma.loyalty.api.admin.serializers import (
    CashbackPolicySerializer,
    PolicyVersionCreatedSerializer,
    PolicyVersionCreateSerializer,
)


class CashbackPolicyView(AdminAPIView):
    @CASHBACK_POLICY_GET
    def get(self, request: Request) -> Response:
        now = timeutils.now()
        versions = selectors.policy_versions()
        by_state: dict[str, Any] = {}
        for policy in versions:
            by_state.setdefault(policy.state_at(now), policy)
        payload = {
            "active": by_state.get(enums.PolicyVersionState.ACTIVE),
            "scheduled": by_state.get(enums.PolicyVersionState.SCHEDULED),
            "versions": versions,
            "next_effective_from": selectors.next_effective_from(now),
        }
        return Response(CashbackPolicySerializer(payload, context={"now": now}).data)


class PolicyVersionCreateView(AdminAPIView):
    @POLICY_VERSION_CREATE_POST
    @idempotent()
    def post(self, request: Request) -> Response:
        serializer = PolicyVersionCreateSerializer(data=cast(dict[str, Any], request.data))
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        try:
            result = services.create_policy_version(
                data["effective_from"],
                data["basis"],
                data["earn_on_cashback_paid"],
                serializer.tier_bands(),
                request.user.pk,
                request=request,
            )
        except services.PolicyValidationError as exc:
            raise ContractValidationError(exc.errors, message=exc.message) from exc
        now = timeutils.now()
        version = next(policy for policy in selectors.policy_versions() if policy.version == result.version)
        payload = {
            "version": version,
            "superseded_version": result.superseded_version,
            "warnings": list(result.warnings),
        }
        return Response(PolicyVersionCreatedSerializer(payload, context={"now": now}).data, status=201)
