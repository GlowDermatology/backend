"""OpenAPI declarations for the admin «Bosh sahifa» (admin contract §2.21): the landing sections staff can
fill by hand."""

from __future__ import annotations

from drf_spectacular.utils import OpenApiParameter, extend_schema

from glowderma.core import enums
from glowderma.core.api.schema import (
    ADMIN_ERRORS,
    IDEMPOTENCY_KEY_HEADER,
)
from glowderma.customers.api.admin.serializers import (
    HomeSectionSerializer,
    HomeSectionsSerializer,
    HomeSectionWriteSerializer,
)

SECTION_KEY = OpenApiParameter(
    "key", str, OpenApiParameter.PATH, enum=enums.HomeSectionKey.values, description="The section."
)

HOME_SECTIONS_GET = extend_schema(
    operation_id="admin_home_sections_list",
    description=(
        "Every landing section in the page's order (spotlight, the `new`, `promo` and `bestsellers` rails, "
        "categories, specialists): its saved picks with their `issue` and what the automatic rule shows now."
    ),
    responses={200: HomeSectionsSerializer, **ADMIN_ERRORS},
)

HOME_SECTION_PUT = extend_schema(
    operation_id="admin_home_sections_update",
    description=(
        "Replaces the section's picks (`[]` = automatic again). Checks: at most `limit` "
        '("Ko‘pi bilan {limit} ta."), each once, every id known (`does_not_exist`), and every pick shows '
        "on the landing now (`home_pick_unavailable`, one error per pick: hidden, no photo for the "
        "spotlight, not on promo for the `promo` rail, an inactive category, an unpublished specialist). "
        "Audited as `home_section.saved`. Returns the section."
    ),
    parameters=[SECTION_KEY, IDEMPOTENCY_KEY_HEADER],
    request=HomeSectionWriteSerializer,
    responses={200: HomeSectionSerializer, **ADMIN_ERRORS},
)


__all__ = [
    "HOME_SECTIONS_GET",
    "HOME_SECTION_PUT",
    "SECTION_KEY",
]
