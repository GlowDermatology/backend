"""``core.phones.normalize_uz_phone`` and ``core.api.fields.LenientPhoneField`` (admin phone leniency)."""

from typing import Any

import pytest
from rest_framework import serializers

from glowderma.core.api.fields import LenientPhoneField, PhoneField
from glowderma.core.phones import normalize_uz_phone

E164 = "+998901234567"


@pytest.mark.parametrize(
    "raw",
    [
        "+998901234567",
        "998901234567",
        "901234567",
        "+998 90 123 45 67",
        "+998 (90) 123-45-67",
        "998-90-123-45-67",
        "(90) 123 45 67",
        "90 123 45 67",
        "90.123.45.67",
        "90/123/45/67",
        "00998901234567",
        "00 998 90 123 45 67",
        "  +998901234567  ",
        "+998 90 123 45 67",  # no-break spaces (copied from a web page)
        "+998 90 123 45 67",  # narrow / thin spaces
        "+998‒90–123—4567",  # figure dash, en dash, em dash
        "+998\t90\n123 45 67",
        "+ 998 90 123 45 67",
    ],
)
def test_accepted_spellings(raw: str) -> None:
    assert normalize_uz_phone(raw) == E164


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "   ",
        "+",
        "90123456",  # 8 national digits
        "9012345678",  # 10
        "99890123456",  # 11
        "9989012345678",  # 13
        "+90123456789",  # "+" without the country code
        "+7 901 234 56 78",  # another country
        "+998 90 123 45 6x",
        "tel:+998901234567",
        "+998+901234567",
        "998901234567#",
        "++998901234567",
        "+998_901234567",
        "+998'90'1234567",
        "٩٩٨٩٠١٢٣٤٥٦٧",  # Arabic-Indic digits are not ASCII digits
        "９０１２３４５６７",  # full-width digits
        "0901234567",  # a trunk "0" is not Uzbek
        "8 90 123 45 67",  # the old Soviet trunk prefix is not accepted (ambiguous)
        "+998 90 123 45 67" + " " * 20,  # longer than 32 characters
        "1" * 1000,
    ],
)
def test_refused_spellings(raw: str) -> None:
    assert normalize_uz_phone(raw) is None


@pytest.mark.parametrize("raw", [None, 901234567, 998901234567, ["+998901234567"], b"+998901234567"])
def test_non_text_is_refused(raw: Any) -> None:
    assert normalize_uz_phone(raw) is None


class Body(serializers.Serializer[dict[str, Any]]):
    phone = LenientPhoneField()
    optional = LenientPhoneField(required=False, allow_null=True)
    phones = serializers.ListField(child=LenientPhoneField(), required=False)


def codes(data: dict[str, Any]) -> dict[str, list[str]]:
    serializer = Body(data=data)
    assert not serializer.is_valid()
    return {name: [str(error.code) for error in errors] for name, errors in serializer.errors.items()}


def test_lenient_field_normalises_and_refuses() -> None:
    serializer = Body(data={"phone": "90 123 45 67", "optional": None, "phones": ["998901234567", E164]})
    assert serializer.is_valid(), serializer.errors
    assert serializer.validated_data == {"phone": E164, "optional": None, "phones": [E164, E164]}
    assert codes({"phone": "123"}) == {"phone": ["invalid"]}
    assert codes({"phone": 901234567}) == {"phone": ["invalid"]}  # a JSON number is never guessed
    assert codes({"phone": ""}) == {"phone": ["blank"]}
    assert codes({"phone": None}) == {"phone": ["null"]}
    assert codes({"phone": "9" * 40}) == {"phone": ["invalid"]}  # not max_length
    message = Body(data={"phone": "x"})
    message.is_valid()
    assert str(message.errors["phone"][0]) == "Telefon raqamini to‘g‘ri kiriting."


def test_the_strict_field_is_unchanged() -> None:
    class Strict(serializers.Serializer[dict[str, Any]]):
        phone = PhoneField()

    assert Strict(data={"phone": E164}).is_valid()
    for raw in ("998901234567", "+998 90 123 45 67", "901234567"):
        assert not Strict(data={"phone": raw}).is_valid(), raw
