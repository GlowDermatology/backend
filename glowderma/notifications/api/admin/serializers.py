"""Admin serializers of `notifications` (contract §2.8: NotificationRecord, meta, templates, segments)."""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any, cast

from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers
from rest_framework.utils import html

from glowderma.accounts.models import Customer
from glowderma.catalog.models import Product
from glowderma.core import enums, timeutils
from glowderma.core.api.exceptions import field_error
from glowderma.core.api.fields import AwareDateTimeField, BoundedListField, PercentField, PhoneField
from glowderma.core.api.serializers import ActorSerializer
from glowderma.core.media import absolute_media_url
from glowderma.notifications import selectors, services
from glowderma.notifications.models import Campaign, CustomerSegment, StatusTemplate

# A ``{`` that does not open ``{raqam}`` (the DB CHECK uses the same pattern).
UNKNOWN_PLACEHOLDER = re.compile(r"\{(?!raqam\})")
MESSAGES = {
    "title_min": "Sarlavhani kiriting.",
    "title_max": "60 belgidan oshmasin.",
    "body_min": "Matnni kiriting.",
    "body_max": "180 belgidan oshmasin.",
    "product_not_found": "Mahsulot topilmadi yoki yashirin.",
    "customer_not_found": "Bu raqam bilan mijoz topilmadi.",
    "in_past": "Kelajakdagi vaqtni tanlang.",
    "unknown_placeholder": "Faqat {raqam} o‘rinbosari ishlatiladi.",
}

# --- NotificationRecord -------------------------------------------------------------------------------------


class NotificationLinkSerializer(serializers.Serializer[dict[str, Any]]):
    kind = serializers.ChoiceField(choices=enums.LinkKind.values)
    product_id = serializers.IntegerField(allow_null=True)
    label = serializers.CharField(allow_null=True)  # type: ignore[assignment]  # a field, not Field.label


class NotificationAudienceSerializer(serializers.Serializer[dict[str, Any]]):
    type = serializers.ChoiceField(choices=enums.AudienceType.values)
    segment_id = serializers.IntegerField(allow_null=True)
    segment_name = serializers.CharField(allow_null=True)
    phone = serializers.CharField(allow_null=True)


def record_link(campaign: Campaign) -> dict[str, Any]:
    return {
        "kind": campaign.link_kind,
        "product_id": campaign.link_product_id,
        "label": campaign.link_product_label or None,
    }


def record_audience(campaign: Campaign) -> dict[str, Any]:
    return {
        "type": campaign.audience_type,
        "segment_id": campaign.audience_segment_id,
        "segment_name": campaign.audience_segment_name or None,
        "phone": campaign.audience_phone or None,
    }


def record_state(campaign: Campaign) -> str:
    """The internal ``sending`` renders as the contract's ``queued``."""
    return enums.NotificationState.QUEUED if campaign.state == Campaign.State.SENDING else campaign.state


class NotificationRecordSerializer(serializers.Serializer[Campaign]):
    id = serializers.IntegerField()
    kind = serializers.ChoiceField(choices=enums.NotificationKind.values)
    title = serializers.CharField()
    body = serializers.CharField()
    image_url = serializers.SerializerMethodField()
    link = serializers.SerializerMethodField()
    audience = serializers.SerializerMethodField()
    recipient_count = serializers.IntegerField()
    state = serializers.SerializerMethodField()
    scheduled_at = serializers.DateTimeField(allow_null=True)
    created_at = serializers.DateTimeField()
    sent_at = serializers.DateTimeField(allow_null=True)
    sent = serializers.IntegerField(allow_null=True)
    failed = serializers.IntegerField(allow_null=True)
    delivered = serializers.IntegerField(allow_null=True)
    author = ActorSerializer(allow_null=True)

    @extend_schema_field(serializers.CharField(allow_null=True))
    def get_image_url(self, obj: Campaign) -> str | None:
        return absolute_media_url(obj.image.name or "") if obj.image else None

    @extend_schema_field(NotificationLinkSerializer())
    def get_link(self, obj: Campaign) -> dict[str, Any]:
        return record_link(obj)

    @extend_schema_field(NotificationAudienceSerializer())
    def get_audience(self, obj: Campaign) -> dict[str, Any]:
        return record_audience(obj)

    @extend_schema_field(serializers.ChoiceField(choices=enums.NotificationState.values))
    def get_state(self, obj: Campaign) -> str:
        return record_state(obj)


class NotificationListQuerySerializer(serializers.Serializer[dict[str, Any]]):
    state = serializers.ChoiceField(choices=enums.NotificationState.values, required=False)
    kind = serializers.ChoiceField(choices=enums.NotificationKind.values, required=False)


# --- meta ---------------------------------------------------------------------------------------------------


class TemplateTextsSerializer(serializers.Serializer[dict[str, str]]):
    uz = serializers.CharField(min_length=5, max_length=180)
    ru = serializers.CharField(max_length=180, allow_blank=True)
    en = serializers.CharField(max_length=180, allow_blank=True)

    def _placeholders(self, value: str) -> str:
        if UNKNOWN_PLACEHOLDER.search(value):
            raise serializers.ValidationError(
                [field_error("unknown_placeholder", MESSAGES["unknown_placeholder"])]
            )
        return value

    def validate_uz(self, value: str) -> str:
        return self._placeholders(value)

    def validate_ru(self, value: str) -> str:
        return self._placeholders(value)

    def validate_en(self, value: str) -> str:
        return self._placeholders(value)


class StatusTemplateSerializer(serializers.Serializer[StatusTemplate]):
    status = serializers.ChoiceField(choices=enums.TemplateStatus.values)
    texts = serializers.SerializerMethodField()
    updated_at = serializers.DateTimeField()
    updated_by = ActorSerializer(allow_null=True)

    @extend_schema_field(TemplateTextsSerializer())
    def get_texts(self, obj: StatusTemplate) -> dict[str, str]:
        return services.template_texts(obj)


class TemplateUpdateSerializer(serializers.Serializer[dict[str, Any]]):
    texts = TemplateTextsSerializer()


class MetaSegmentSerializer(serializers.Serializer[dict[str, Any]]):
    id = serializers.IntegerField()
    name = serializers.CharField()
    size = serializers.IntegerField()


class LinkOptionSerializer(serializers.Serializer[dict[str, Any]]):
    product_id = serializers.IntegerField()
    label = serializers.CharField()  # type: ignore[assignment]  # a field, not Field.label


class NotificationMetaSerializer(serializers.Serializer[dict[str, Any]]):
    total_devices = serializers.IntegerField()
    segments = MetaSegmentSerializer(many=True)
    templates = StatusTemplateSerializer(many=True)
    link_options = LinkOptionSerializer(many=True)


# --- POST notifications/ (multipart) ------------------------------------------------------------------------


class NotificationLinkInputSerializer(serializers.Serializer[dict[str, Any]]):
    kind = serializers.ChoiceField(choices=enums.LinkKind.values, default=enums.LinkKind.NONE)
    product_id = serializers.IntegerField(required=False, allow_null=True)


class NotificationAudienceInputSerializer(serializers.Serializer[dict[str, Any]]):
    type = serializers.ChoiceField(choices=enums.AudienceType.values)
    segment_id = serializers.IntegerField(required=False, allow_null=True)
    phone = PhoneField(required=False, allow_null=True)


class NotificationCreateSerializer(serializers.Serializer[dict[str, Any]]):
    """The multipart body of ``POST notifications/`` after :func:`form_to_nested` (dotted keys un-dotted)."""

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
        if value is not None and value <= timeutils.now():
            raise serializers.ValidationError([field_error("in_past", MESSAGES["in_past"])])
        return value

    def validate(self, attrs: dict[str, Any]) -> dict[str, Any]:
        errors: dict[str, Any] = {}
        link = attrs.get("link") or {"kind": enums.LinkKind.NONE}
        product: Product | None = None
        if link["kind"] == enums.LinkKind.PRODUCT:
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
            audience_type=data["audience"]["type"],
            audience_segment=data["audience_segment"],
            audience_customer=data["audience_customer"],
            scheduled_at=data.get("scheduled_at"),
        )


# --- segments -----------------------------------------------------------------------------------------------


class SegmentRulesSerializer(serializers.Serializer[dict[str, Any]]):
    min_online_orders = serializers.IntegerField(min_value=1, allow_null=True, required=False)
    min_lifetime_amount = serializers.IntegerField(min_value=1, allow_null=True, required=False)


class SegmentSerializer(serializers.Serializer[CustomerSegment]):
    """Contract §2.8 ``Segment``; the counts come from ``selectors.segment_stats``."""

    id = serializers.IntegerField()
    name = serializers.CharField()
    rules = serializers.SerializerMethodField()
    manual_member_count = serializers.SerializerMethodField()
    member_count = serializers.SerializerMethodField()
    size = serializers.SerializerMethodField()
    created_at = serializers.DateTimeField()
    updated_at = serializers.DateTimeField()

    def _stats(self, obj: CustomerSegment) -> selectors.SegmentStats:
        context = cast(dict[str, Any], self.context)
        cache: dict[int, selectors.SegmentStats] = context.setdefault("segment_stats", {})
        if obj.pk not in cache:
            cache[obj.pk] = selectors.segment_stats(obj)
        return cache[obj.pk]

    @extend_schema_field(SegmentRulesSerializer())
    def get_rules(self, obj: CustomerSegment) -> dict[str, Any]:
        return {"min_online_orders": obj.min_online_orders, "min_lifetime_amount": obj.min_lifetime_amount}

    @extend_schema_field(serializers.IntegerField())
    def get_manual_member_count(self, obj: CustomerSegment) -> int:
        return self._stats(obj)["manual_member_count"]

    @extend_schema_field(serializers.IntegerField())
    def get_member_count(self, obj: CustomerSegment) -> int:
        return self._stats(obj)["member_count"]

    @extend_schema_field(serializers.IntegerField())
    def get_size(self, obj: CustomerSegment) -> int:
        return self._stats(obj)["size"]


class SegmentWriteSerializer(serializers.Serializer[dict[str, Any]]):
    """``POST segments/`` (``name`` 2..60 unique, ``rules``) and ``PATCH segments/{id}/`` (partial)."""

    name = serializers.CharField(min_length=2, max_length=60)
    rules = SegmentRulesSerializer(required=False)

    def __init__(self, *args: Any, instance: CustomerSegment | None = None, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.segment = instance

    def validate_name(self, value: str) -> str:
        value = value.strip()
        if len(value) < 2:
            raise serializers.ValidationError([field_error("min_length", limit=2)])
        taken = CustomerSegment.objects.filter(name__iexact=value)
        if self.segment is not None:
            taken = taken.exclude(pk=self.segment.pk)
        if taken.exists():
            raise serializers.ValidationError([field_error("unique")])
        return value

    def to_changes(self) -> dict[str, Any]:
        data = self.validated_data
        changes: dict[str, Any] = {}
        if "name" in data:
            changes["name"] = data["name"]
        for key in ("min_online_orders", "min_lifetime_amount"):
            if key in data.get("rules", {}):
                changes[key] = data["rules"][key]
        return changes


class CustomerIdsField(BoundedListField):
    """``customer_ids``: DRF's ``not_a_list`` / ``empty`` are not contract codes (and carry English stock
    text), so they answer the generic ``invalid`` / ``required`` (contract §1.6)."""

    def to_internal_value(self, data: Any) -> list[Any]:
        if html.is_html_input(data):
            data = html.parse_html_list(data, default=[])
        if isinstance(data, (str, bytes, Mapping)) or not hasattr(data, "__iter__"):
            raise serializers.ValidationError([field_error("invalid")])
        if len(data) == 0:
            raise serializers.ValidationError([field_error("required")])
        return super().to_internal_value(data)


class SegmentMembersInputSerializer(serializers.Serializer[dict[str, Any]]):
    customer_ids = CustomerIdsField(child=serializers.IntegerField(), max_items=500)

    def validate_customer_ids(self, value: list[int]) -> list[int]:
        ids = sorted(set(value))
        known = set(Customer.objects.filter(pk__in=ids).values_list("pk", flat=True))
        if len(known) != len(ids):
            raise serializers.ValidationError([field_error("does_not_exist")])
        return ids


class CustomerListItemSerializer(serializers.Serializer[Customer]):
    """Contract §2.6 ``CustomerListItem``: the one renderer of ``GET customers/`` and of segment members.

    Rows carry the ``loyalty.selectors.list_annotations()`` values (``lifetime_total``, ``balance``,
    ``percent``, ``online_orders_count``).
    """

    id = serializers.IntegerField()
    name = serializers.CharField(allow_blank=True)
    phone = serializers.CharField()
    status = serializers.ChoiceField(choices=enums.CustomerStatus.values)
    registered_at = serializers.DateTimeField()
    lifetime_total = serializers.IntegerField()
    percent = PercentField(allow_null=True)
    balance = serializers.IntegerField()
    online_orders_count = serializers.IntegerField()
