from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest
from rest_framework import serializers
from rest_framework.permissions import AllowAny
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.test import APIRequestFactory
from rest_framework.views import APIView

from glowderma.core.api.fields import (
    AwareDateTimeField,
    BoundedListField,
    HttpsUrlField,
    PercentField,
    PhoneField,
    QueryBooleanField,
    SlugField,
    i18n_dict,
    i18n_text,
)


def errors(serializer: serializers.Serializer[Any]) -> list[dict[str, Any]]:
    """Run the serializer through a view so the envelope handler flattens its errors."""

    class RaisingView(APIView):
        authentication_classes: list[Any] = []
        permission_classes = [AllowAny]

        def get(self, request: Request) -> Response:
            serializer.is_valid(raise_exception=True)
            return Response(serializer.validated_data)

    response = RaisingView.as_view()(APIRequestFactory().get("/api/test/"))
    assert response.status_code == 400
    assert response.data["code"] == "validation_error"
    return list(response.data["errors"])


def single(field: serializers.Field[Any, Any, Any, Any], data: dict[str, Any]) -> serializers.Serializer[Any]:
    class InputSerializer(serializers.Serializer[dict[str, Any]]):
        value = field

    return InputSerializer(data=data)


class TestAwareDateTimeField:
    def test_naive_input_is_invalid(self) -> None:
        """Contract 1.4: "ISO 8601 with an offset (`Z` or `±HH:MM`); naive values → `invalid`"."""
        assert errors(single(AwareDateTimeField(), {"value": "2026-11-01T00:00:00"})) == [
            {"field": "value", "code": "invalid", "message": "Noto‘g‘ri qiymat."}
        ]
        assert errors(single(AwareDateTimeField(), {"value": "yesterday"}))[0]["code"] == "invalid"

    @pytest.mark.parametrize("raw", ["2026-11-01T00:00:00+05:00", "2026-10-31T19:00:00Z"])
    def test_aware_input_is_accepted(self, raw: str) -> None:
        serializer = single(AwareDateTimeField(), {"value": raw})
        assert serializer.is_valid(), serializer.errors
        assert serializer.validated_data["value"] == datetime(2026, 10, 31, 19, 0, tzinfo=UTC)

    def test_naive_datetime_object_is_invalid(self) -> None:
        serializer = single(AwareDateTimeField(), {"value": datetime(2026, 11, 1)})  # noqa: DTZ001
        assert not serializer.is_valid()
        assert serializer.errors["value"][0].code == "invalid"


class TestPercentField:
    def test_string_with_two_places_renders_two_places(self) -> None:
        serializer = single(PercentField(), {"value": "1.5"})
        assert serializer.is_valid(), serializer.errors
        assert serializer.validated_data["value"] == Decimal("1.5")
        assert single(PercentField(), {}).to_representation({"value": Decimal("1.5")}) == {"value": "1.50"}

    def test_json_number_is_invalid(self) -> None:
        """Contract 1.4: "Requests allow ≤2 dp; more → `max_decimal_places`; a JSON number → `invalid`"."""
        for number in (1, 1.5):
            assert errors(single(PercentField(), {"value": number})) == [
                {"field": "value", "code": "invalid", "message": "Noto‘g‘ri qiymat."}
            ]
        assert errors(single(PercentField(), {"value": "abc"}))[0]["code"] == "invalid"

    def test_three_places_is_max_decimal_places(self) -> None:
        assert errors(single(PercentField(), {"value": "1.234"})) == [
            {"field": "value", "code": "max_decimal_places", "message": "Ko‘pi bilan 2 ta kasr raqam."}
        ]

    @pytest.mark.parametrize("raw", ["0", "0.00", "100.01", "-1"])
    def test_out_of_range(self, raw: str) -> None:
        """Contract 2.7: `percent ∉ (0, 100]` → `tiers.{i}.percent` `out_of_range`."""
        field = PercentField(exclusive_min=0, inclusive_max=100)
        assert errors(single(field, {"value": raw})) == [
            {"field": "value", "code": "out_of_range", "message": "Qiymat 0 dan katta va 100 dan oshmasin."}
        ]

    @pytest.mark.parametrize("raw", ["0.01", "100", "100.00", "57.5"])
    def test_in_range(self, raw: str) -> None:
        serializer = single(PercentField(exclusive_min=0, inclusive_max=100), {"value": raw})
        assert serializer.is_valid(), serializer.errors

    def test_single_bound_messages_and_override(self) -> None:
        assert errors(single(PercentField(exclusive_min="0"), {"value": "0"}))[0]["message"] == (
            "Qiymat 0 dan katta bo‘lsin."
        )
        assert errors(single(PercentField(inclusive_max=5), {"value": "5.01"}))[0]["message"] == (
            "Qiymat 5 dan oshmasin."
        )
        custom = PercentField(
            exclusive_min=0,
            inclusive_max=100,
            error_messages={"out_of_range": "Foiz 0 dan katta va 100 dan oshmasligi kerak."},
        )
        assert errors(single(custom, {"value": "101"})) == [
            {
                "field": "value",
                "code": "out_of_range",
                "message": "Foiz 0 dan katta va 100 dan oshmasligi kerak.",
            }
        ]


class TestPhoneField:
    @pytest.mark.parametrize("raw", ["+998901234567", " +998901234567 "])
    def test_valid(self, raw: str) -> None:
        serializer = single(PhoneField(), {"value": raw})
        assert serializer.is_valid(), serializer.errors
        assert serializer.validated_data["value"] == "+998901234567"

    @pytest.mark.parametrize(
        "raw", ["998901234567", "+99890123456", "+9989012345678", "+7 900 123-45-67", "abc"]
    )
    def test_invalid(self, raw: str) -> None:
        """Contract 1.4: Phone `^\\+998\\d{9}$`; 2.8: `invalid` "Telefon raqamini to‘g‘ri kiriting."."""
        assert errors(single(PhoneField(), {"value": raw})) == [
            {"field": "value", "code": "invalid", "message": "Telefon raqamini to‘g‘ri kiriting."}
        ]


class TestHttpsUrlField:
    """Banner and campaign destinations (gaps X-BANNER-URL, ADM-10.1; decision GC-3)."""

    @pytest.mark.parametrize(
        "raw",
        [
            "https://glowderma.uz",
            " https://glowderma.uz/aksiya?utm=app#top ",
            "https://t.me/glowderma",
            "https://glowderma.uz:443/x",
            "https://xn--80ak6aa92e.com/",
        ],
    )
    def test_valid(self, raw: str) -> None:
        serializer = single(HttpsUrlField(), {"value": raw})
        assert serializer.is_valid(), serializer.errors
        assert serializer.validated_data["value"] == raw.strip()

    @pytest.mark.parametrize(
        "raw",
        [
            "http://glowderma.uz",
            "glowderma.uz",
            "//glowderma.uz",
            "javascript:alert(1)",
            "https://",
            "https://user:pass@glowderma.uz/",
            "https://user@glowderma.uz/",
            "https://glowderma.uz:8443/",
            "https://glowderma.uz:99999/",
            "https://glow derma.uz/",
            "ftp://glowderma.uz/",
        ],
    )
    def test_invalid(self, raw: str) -> None:
        assert errors(single(HttpsUrlField(), {"value": raw})) == [
            {"field": "value", "code": "invalid", "message": "Havolani https:// bilan to‘liq kiriting."}
        ]

    def test_max_length(self) -> None:
        raw = "https://glowderma.uz/" + "a" * 480
        assert errors(single(HttpsUrlField(), {"value": raw}))[0]["code"] == "max_length"
        assert single(HttpsUrlField(), {"value": raw[:-1]}).is_valid()


class TestSlugField:
    @pytest.mark.parametrize("raw", ["serums", "a1", "x" * 40, "face-care-2"])
    def test_valid(self, raw: str) -> None:
        assert single(SlugField(), {"value": raw}).is_valid()

    @pytest.mark.parametrize("raw", ["a", "x" * 41, "Serums", "sérum", "face_care", "face care"])
    def test_pattern(self, raw: str) -> None:
        """Contract 1.4: Category is addressed by `slug` (`^[a-z0-9-]{2,40}$`)."""
        assert errors(single(SlugField(), {"value": raw}))[0]["code"] == "invalid"

    def test_order_is_reserved(self) -> None:
        """`PUT categories/order/` is a route, so the literal slug "order" can never be created."""
        assert errors(single(SlugField(), {"value": "order"})) == [
            {"field": "value", "code": "invalid", "message": "Bu slug band."}
        ]


class TestBoundedListField:
    def test_max_items(self) -> None:
        """Contract 2.3: ≤8 images per product (`max_items`); 1.6: `max_items` "Ko‘pi bilan {n} ta."."""
        field = BoundedListField(max_items=3, child=serializers.IntegerField())
        assert errors(single(field, {"value": [1, 2, 3, 4]})) == [
            {"field": "value", "code": "max_items", "message": "Ko‘pi bilan 3 ta."}
        ]
        serializer = single(
            BoundedListField(max_items=3, child=serializers.IntegerField()), {"value": [1, 2, 3]}
        )
        assert serializer.is_valid() and serializer.validated_data["value"] == [1, 2, 3]

    def test_other_list_errors_are_kept(self) -> None:
        field = BoundedListField(max_items=3, child=serializers.IntegerField())
        assert errors(single(field, {"value": "nope"}))[0]["code"] == "not_a_list"
        assert errors(
            single(BoundedListField(max_items=3, child=serializers.IntegerField()), {"value": [1, "x"]})
        ) == [{"field": "value.1", "code": "invalid", "message": "Noto‘g‘ri qiymat."}]


class TestQueryBooleanField:
    @pytest.mark.parametrize(
        ("raw", "expected"), [("true", True), ("1", True), ("false", False), ("0", False)]
    )
    def test_accepted_literals(self, raw: str, expected: bool) -> None:
        """Contract 1.4: Query booleans `true` / `false` (also `1` / `0`)."""
        serializer = single(QueryBooleanField(), {"value": raw})
        assert serializer.is_valid(), serializer.errors
        assert serializer.validated_data["value"] is expected
        assert single(QueryBooleanField(), {"value": expected}).is_valid()

    @pytest.mark.parametrize("raw", ["yes", "True", "", "2", "on"])
    def test_other_values_are_invalid_choice(self, raw: str) -> None:
        assert errors(single(QueryBooleanField(), {"value": raw})) == [
            {"field": "value", "code": "invalid_choice", "message": "Ruxsat etilmagan qiymat."}
        ]


class TestI18nText:
    def test_nested_paths_and_uz_minimum(self) -> None:
        """Contract 1.4: i18n text is `{"uz","ru","en"}`; `i18n_text` is field-first (categories.name,
        banners.title, notification-templates.texts), so paths flatten as `texts.name.uz`. The product
        `texts` object of 2.3 is language-first (`texts.uz.name`) and needs its own serializer."""

        class NameSerializer(serializers.Serializer[dict[str, Any]]):
            name = i18n_text(120, uz_min_length=2)

        class ProductSerializer(serializers.Serializer[dict[str, Any]]):
            texts = NameSerializer()

        flattened = errors(ProductSerializer(data={"texts": {"name": {"uz": "x", "ru": "", "en": ""}}}))
        assert flattened == [
            {"field": "texts.name.uz", "code": "min_length", "message": "Kamida 2 ta belgi kiriting."}
        ]
        assert errors(ProductSerializer(data={"texts": {"name": {"uz": "", "ru": "", "en": ""}}})) == [
            {"field": "texts.name.uz", "code": "blank", "message": "Maydonni to‘ldiring."}
        ]
        assert (
            errors(ProductSerializer(data={"texts": {"name": {"uz": "ok"}}}))[0]["field"] == "texts.name.ru"
        )
        assert errors(ProductSerializer(data={"texts": {}})) == [
            {"field": "texts.name", "code": "required", "message": "Majburiy maydon."}
        ]

    def test_blank_allowed_without_minimum_and_trimmed(self) -> None:
        class BodySerializer(serializers.Serializer[dict[str, Any]]):
            description = i18n_text(500)
            subtitle = i18n_text(60, required=False)

        serializer = BodySerializer(data={"description": {"uz": "  salom ", "ru": "", "en": ""}})
        assert serializer.is_valid(), serializer.errors
        assert serializer.validated_data == {"description": {"uz": "salom", "ru": "", "en": ""}}
        assert errors(BodySerializer(data={"description": {"uz": "x" * 501, "ru": "", "en": ""}})) == [
            {"field": "description.uz", "code": "max_length", "message": "500 belgidan oshmasin."}
        ]

    def test_component_name_encodes_the_constraints(self) -> None:
        assert type(i18n_text(120, uz_min_length=2)).__name__ == "I18nText120Min2"
        assert type(i18n_text(500)).__name__ == "I18nText500"
        assert type(i18n_text(500)) is type(i18n_text(500))

    def test_i18n_dict_never_returns_null(self) -> None:
        class Row:
            name_uz = "Serum"
            name_ru = None
            name_en = ""

        assert i18n_dict(Row(), "name") == {"uz": "Serum", "ru": "", "en": ""}
