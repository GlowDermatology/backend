"""The campaign draft writes: the multipart ``POST notifications/`` body, the PATCH meta and the list
query."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, cast

from rest_framework import serializers

from glowderma.accounts.models import Customer
from glowderma.catalog.models import Product
from glowderma.core import enums, timeutils
from glowderma.core.api.exceptions import field_error
from glowderma.core.api.fields import (
    AwareDateTimeField,
    HttpsUrlField,
    LenientPhoneField,
)
from glowderma.notifications import services
from glowderma.notifications.api.admin.serializers.messages import MESSAGES
from glowderma.notifications.models import CustomerSegment


class NotificationPatchMetaSerializer(serializers.Serializer[dict[str, Any]]):
    """The non-content keys of the multipart ``PATCH notifications/{id}/`` body."""

    expected_revision = serializers.IntegerField(min_value=1)
    remove_image = serializers.BooleanField(required=False, default=False)


class NotificationListQuerySerializer(serializers.Serializer[dict[str, Any]]):
    state = serializers.ChoiceField(choices=enums.NotificationState.values, required=False)
    kind = serializers.ChoiceField(choices=enums.NotificationKind.values, required=False)


class NotificationLinkInputSerializer(serializers.Serializer[dict[str, Any]]):
    kind = serializers.ChoiceField(choices=enums.LinkKind.values, default=enums.LinkKind.NONE)
    product_id = serializers.IntegerField(required=False, allow_null=True)
    # gap ADM-10.1 (decision GC-3): required for kind "url"; "" counts as missing; ignored for other kinds
    url = HttpsUrlField(required=False, allow_null=True, allow_blank=True)

    def to_internal_value(self, data: Any) -> dict[str, Any]:
        # a stray link.url next to another kind is ignored, so it is not validated either (review nit)
        if isinstance(data, Mapping) and data.get("kind") != enums.LinkKind.URL and "url" in data:
            data = {key: value for key, value in data.items() if key != "url"}
        return cast(dict[str, Any], super().to_internal_value(data))


class NotificationAudienceInputSerializer(serializers.Serializer[dict[str, Any]]):
    type = serializers.ChoiceField(choices=enums.AudienceType.values)
    segment_id = serializers.IntegerField(required=False, allow_null=True)
    phone = LenientPhoneField(required=False, allow_null=True)


class NotificationCreateSerializer(serializers.Serializer[dict[str, Any]]):
    """The multipart body of ``POST notifications/`` after :func:`form_to_nested` (dotted keys un-dotted);
    ``PATCH notifications/{id}/`` validates the stored form merged with the submitted keys through it too
    (:func:`glowderma.notifications.api.admin.views.merged_form`)."""

    kind = serializers.ChoiceField(choices=enums.NotificationKind.values)
    # ``allow_blank``: DRF trims first and would answer ``blank`` for "" / "   "; the contract wants the
    # ``min_length`` copy for every trimmed length below the minimum, the empty string included.
    title = serializers.CharField(
        max_length=60,
        min_length=3,
        allow_blank=True,
        error_messages={"min_length": MESSAGES["title_min"], "max_length": MESSAGES["title_max"]},
    )
    body = serializers.CharField(
        max_length=180,
        min_length=5,
        allow_blank=True,
        error_messages={"min_length": MESSAGES["body_min"], "max_length": MESSAGES["body_max"]},
    )
    link = NotificationLinkInputSerializer(required=False)
    audience = NotificationAudienceInputSerializer()
    scheduled_at = AwareDateTimeField(required=False, allow_null=True)

    def to_internal_value(self, data: Any) -> dict[str, Any]:
        # A form without any ``audience.*`` key reports ``audience.type`` (the multipart name).
        if isinstance(data, dict) and "audience" not in data:
            data = {**data, "audience": {}}
        return cast(dict[str, Any], super().to_internal_value(data))

    def validate_title(self, value: str) -> str:
        if value == "":
            raise serializers.ValidationError([field_error("min_length", MESSAGES["title_min"])])
        return value

    def validate_body(self, value: str) -> str:
        if value == "":
            raise serializers.ValidationError([field_error("min_length", MESSAGES["body_min"])])
        return value

    def validate_scheduled_at(self, value: Any) -> Any:
        # ``in_past`` is for a time the client chose: a PATCH that leaves the stored requested time alone
        # passes ``context["check_scheduled_at"] = False`` (the dispatch waits for ``max(now + delay, it)``).
        if value is not None and self.context.get("check_scheduled_at", True) and value <= timeutils.now():
            raise serializers.ValidationError([field_error("in_past", MESSAGES["in_past"])])
        return value

    def validate(self, attrs: dict[str, Any]) -> dict[str, Any]:
        errors: dict[str, Any] = {}
        link = attrs.get("link") or {"kind": enums.LinkKind.NONE}
        product: Product | None = None
        url = ""
        if link["kind"] == enums.LinkKind.URL:
            url = link.get("url") or ""
            if not url:
                errors["link"] = {"url": [field_error("required")]}
        elif link["kind"] == enums.LinkKind.PRODUCT:
            if link.get("product_id") is None:
                errors["link"] = {"product_id": [field_error("required")]}
            else:
                product = Product.objects.filter(
                    pk=link["product_id"], status=enums.ProductStatus.ACTIVE
                ).first()
                if product is None:
                    errors["link"] = {
                        "product_id": [field_error("does_not_exist", MESSAGES["product_not_found"])]
                    }
        audience = attrs["audience"]
        segment: CustomerSegment | None = None
        customer: Customer | None = None
        if audience["type"] == enums.AudienceType.SEGMENT:
            if audience.get("segment_id") is None:
                errors["audience"] = {"segment_id": [field_error("required")]}
            else:
                segment = CustomerSegment.objects.filter(pk=audience["segment_id"]).first()
                if segment is None:
                    errors["audience"] = {"segment_id": [field_error("does_not_exist")]}
        elif audience["type"] == enums.AudienceType.CUSTOMER:
            if audience.get("phone") is None:
                errors["audience"] = {"phone": [field_error("required")]}
            else:
                customer = Customer.objects.filter(
                    phone=audience["phone"], status=enums.CustomerStatus.ACTIVE
                ).first()
                if customer is None:
                    errors["audience"] = {
                        "phone": [field_error("customer_not_found", MESSAGES["customer_not_found"])]
                    }
        if errors:
            raise serializers.ValidationError(errors)
        attrs["link_product"] = product
        attrs["link_kind"] = link["kind"]
        attrs["link_url"] = url
        attrs["audience_segment"] = segment
        attrs["audience_customer"] = customer
        return attrs

    def to_input(self) -> services.CampaignInput:
        data = self.validated_data
        return services.CampaignInput(
            kind=data["kind"],
            title=data["title"],
            body=data["body"],
            link_kind=data["link_kind"],
            link_product=data["link_product"],
            link_url=data["link_url"],
            audience_type=data["audience"]["type"],
            audience_segment=data["audience_segment"],
            audience_customer=data["audience_customer"],
            scheduled_at=data.get("scheduled_at"),
        )


__all__ = [
    "NotificationAudienceInputSerializer",
    "NotificationCreateSerializer",
    "NotificationLinkInputSerializer",
    "NotificationListQuerySerializer",
    "NotificationPatchMetaSerializer",
]
