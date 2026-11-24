"""OpenAPI declarations for admin views of `loyalty`: ``GET cashback/policy/`` and ``POST
cashback/policy-versions/`` (contract §2.7).

Every rule lives in ``loyalty.services`` / ``loyalty.selectors``; the views only shape requests and responses.
``PolicyValidationError`` carries the §2.7 field errors (paths ``tiers.{i}.…`` with the submitted index) and
the table's top-level message, raised here as a ``ContractValidationError``."""

from __future__ import annotations

from drf_spectacular.utils import extend_schema

from glowderma.core.api.schema import ADMIN_ERRORS, IDEMPOTENCY_KEY_HEADER
from glowderma.loyalty.api.admin.serializers import (
    CashbackPolicySerializer,
    PolicyVersionCreatedSerializer,
    PolicyVersionCreateSerializer,
)

CASHBACK_POLICY_GET = extend_schema(
    operation_id="admin_cashback_policy_retrieve",
    description=(
        "The active and the scheduled version (at most one each), every version newest first by "
        "`version` (superseded ones included) and `next_effective_from`, the start of the next Tashkent "
        "calendar month."
    ),
    responses={200: CashbackPolicySerializer, **ADMIN_ERRORS},
)

POLICY_VERSION_CREATE_POST = extend_schema(
    operation_id="admin_cashback_policy_versions_create",
    description=(
        "Appends a scheduled version. Tier errors use the submitted index (`tiers.{i}.from|to|percent`); "
        "gaps and an upper-bounded last tier are returned as `warnings`. A previously scheduled version "
        "becomes superseded (`superseded_version`)."
    ),
    parameters=[IDEMPOTENCY_KEY_HEADER],
    request=PolicyVersionCreateSerializer,
    responses={201: PolicyVersionCreatedSerializer, **ADMIN_ERRORS},
)


__all__ = [
    "CASHBACK_POLICY_GET",
    "POLICY_VERSION_CREATE_POST",
]
