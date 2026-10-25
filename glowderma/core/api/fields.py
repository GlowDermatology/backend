"""Shared DRF serializer fields implementing the value rules of admin API v1, section 1.4."""

from datetime import datetime
from decimal import Decimal
from functools import cache
from typing import Any, cast

from rest_framework import serializers

from glowderma.core.api.exceptions import FIELD_MESSAGES, field_error

PHONE_PATTERN = r"^\+998\d{9}$"
SLUG_PATTERN = r"^[a-z0-9-]{2,40}$"
# ``order`` is a route segment (``categories/order/``); ``none`` is the "no category" filter keyword (2.3).
RESERVED_SLUGS = frozenset({"order", "none"})
LANGUAGES = ("uz", "ru", "en")


class AwareDateTimeField(serializers.DateTimeField):
    """Contract 1.4: "ISO 8601 with an offset (`Z` or `±HH:MM`); naive values → `invalid`"."""

    def enforce_timezone(self, value: datetime) -> datetime:
        if value.tzinfo is None or value.tzinfo.utcoffset(value) is None:
            raise serializers.ValidationError(field_error("invalid"))
        return super().enforce_timezone(value)


class PercentField(serializers.DecimalField):
    """Contract 1.4 percent: decimal string, "≤2 dp; more → `max_decimal_places`; a JSON number → `invalid`".

    ``exclusive_min`` / ``inclusive_max`` add the range check of the cashback tiers
    (``percent ∉ (0, 100]`` → ``out_of_range``).
    """

    def __init__(
        self,
        *,
        exclusive_min: Decimal | int | str | None = None,
        inclusive_max: Decimal | int | str | None = None,
        **kwargs: Any,
    ) -> None:
        kwargs.setdefault("max_digits", 5)
        kwargs.setdefault("decimal_places", 2)
        kwargs.setdefault("coerce_to_string", True)
        self.exclusive_min = None if exclusive_min is None else Decimal(str(exclusive_min))
        self.inclusive_max = None if inclusive_max is None else Decimal(str(inclusive_max))
        super().__init__(**kwargs)
        self.error_messages.setdefault("out_of_range", self._range_message())

    def _range_message(self) -> str:
        if self.exclusive_min is not None and self.inclusive_max is not None:
            return f"Qiymat {self.exclusive_min} dan katta va {self.inclusive_max} dan oshmasin."
        if self.exclusive_min is not None:
            return f"Qiymat {self.exclusive_min} dan katta bo‘lsin."
        if self.inclusive_max is not None:
            return f"Qiymat {self.inclusive_max} dan oshmasin."
        return FIELD_MESSAGES["invalid"]

    def to_internal_value(self, data: Any) -> Decimal:
        if isinstance(data, (int, float)):
            self.fail("invalid")
        value = super().to_internal_value(data)
        if self.exclusive_min is not None and value <= self.exclusive_min:
            self.fail("out_of_range")
        if self.inclusive_max is not None and value > self.inclusive_max:
            self.fail("out_of_range")
        return value


class PhoneField(serializers.RegexField):
    """Contract 1.4: phone is E.164 ``^\\+998\\d{9}$``; anything else → ``invalid``."""

    default_error_messages = {"invalid": "Telefon raqamini to‘g‘ri kiriting."}

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(PHONE_PATTERN, **kwargs)


class SlugField(serializers.RegexField):
    """Contract 1.4: a slug matches ``^[a-z0-9-]{2,40}$``; ``order`` is a route segment, never a slug."""

    default_error_messages = {
        "invalid": "Slug faqat kichik lotin harflari, raqamlar va chiziqchadan iborat bo‘lsin (2–40 belgi).",
        "reserved": "Bu slug band.",
    }

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(SLUG_PATTERN, **kwargs)

    def to_internal_value(self, data: Any) -> str:
        value = super().to_internal_value(data)
        if value in RESERVED_SLUGS:
            raise serializers.ValidationError(self.error_messages["reserved"], code="invalid")
        return value


class BoundedListField(serializers.ListField):
    """A list with at most ``max_items`` elements; more → code ``max_items`` "Ko‘pi bilan {n} ta."."""

    def __init__(self, *, max_items: int, **kwargs: Any) -> None:
        self.max_items = max_items
        kwargs.setdefault("max_length", max_items)
        super().__init__(**kwargs)

    def to_internal_value(self, data: Any) -> list[Any]:
        if isinstance(data, (list, tuple)) and len(data) > self.max_items:
            raise serializers.ValidationError(field_error("max_items", limit=self.max_items))
        return super().to_internal_value(data)


class QueryBooleanField(serializers.BooleanField):
    """Contract 1.4: query booleans are ``true`` / ``false`` (also ``1`` / ``0``); else ``invalid_choice``."""

    default_error_messages = {"invalid_choice": FIELD_MESSAGES["invalid_choice"]}

    def to_internal_value(self, data: Any) -> bool:
        if isinstance(data, bool):
            return data
        if data in ("true", "1"):
            return True
        if data in ("false", "0"):
            return False
        self.fail("invalid_choice")
        raise AssertionError("unreachable")  # pragma: no cover


@cache
def _i18n_text_class(max_length: int, uz_min_length: int | None) -> type[serializers.Serializer[Any]]:
    # The class name becomes the OpenAPI component name, so each constraint set gets its own component.
    name = f"I18nText{max_length}" + ("" if uz_min_length is None else f"Min{uz_min_length}")
    uz_kwargs: dict[str, Any] = {"max_length": max_length, "allow_blank": uz_min_length is None}
    if uz_min_length is not None:
        uz_kwargs["min_length"] = uz_min_length
    attrs: dict[str, Any] = {
        "__module__": __name__,
        "__doc__": 'Contract 1.4: i18n text is {"uz", "ru", "en"}; "" means not filled, never null.',
        "uz": serializers.CharField(**uz_kwargs),
        "ru": serializers.CharField(max_length=max_length, allow_blank=True),
        "en": serializers.CharField(max_length=max_length, allow_blank=True),
    }
    return cast(type[serializers.Serializer[Any]], type(name, (serializers.Serializer,), attrs))


def i18n_text(
    max_length: int, uz_min_length: int | None = None, required: bool = True, **kwargs: Any
) -> serializers.Serializer[Any]:
    """A nested ``{"uz", "ru", "en"}`` serializer; ``uz`` becomes mandatory when ``uz_min_length`` is set."""
    return _i18n_text_class(max_length, uz_min_length)(required=required, **kwargs)


def i18n_dict(obj: Any, prefix: str) -> dict[str, str]:
    """Render ``<prefix>_uz`` / ``_ru`` / ``_en`` columns as the contract i18n object (``""``, never null)."""
    return {language: getattr(obj, f"{prefix}_{language}", None) or "" for language in LANGUAGES}
