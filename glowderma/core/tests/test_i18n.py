"""The customer message catalog and language resolution (customer contract §1.2, §5)."""

import re
from pathlib import Path
from types import SimpleNamespace

import pytest
from django.conf import settings
from django.test import RequestFactory

from glowderma.core import i18n
from glowderma.core.api.exceptions import ENDPOINT_FIELD_CODES, FIELD_MESSAGES

CONTRACT = Path(settings.BASE_DIR) / "docs/api/customer-api-v1.md"
KEY_PATTERN = re.compile(r"`((?:error|field|label|push|sms)\.[A-Za-z0-9_.]+)`")
# Outbox topics written like keys in the contract prose, not catalog keys.
TOPICS = {"push.account_security", "push.order_status", "push.session_replaced", "sms.send"}


def test_every_key_has_three_texts_with_the_same_placeholders() -> None:
    assert len(i18n.CATALOG) >= 180
    for key, texts in i18n.CATALOG.items():
        assert set(texts) == {"uz", "ru", "en"}, key
        assert all(text.strip() for text in texts.values()), key
        assert (
            i18n.placeholders(texts["uz"]) == i18n.placeholders(texts["ru"]) == i18n.placeholders(texts["en"])
        ), key


def test_every_key_named_by_the_contract_exists() -> None:
    """§5: "a contract test asserts every key named in §2–§4 exists" (and every row of the §5 tables)."""
    text = CONTRACT.read_text()
    named = set(KEY_PATTERN.findall(text[: text.index("## 6. Security notes")])) - TOPICS
    assert named, "the contract names catalog keys"
    assert sorted(named - set(i18n.CATALOG)) == []


def test_placeholders_are_the_contract_ones() -> None:
    allowed = {"n", "m", "limit", "max", "available", "order_id", "provider", "amount", "percent"}
    allowed |= {"item_count", "total", "tracking_number", "code"}
    used = set().union(*(i18n.placeholders(texts["uz"]) for texts in i18n.CATALOG.values()))
    assert used <= allowed
    sms_only = [key for key, texts in i18n.CATALOG.items() if "code" in i18n.placeholders(texts["uz"])]
    assert sms_only == ["sms.otp"]


def test_generic_field_messages_are_the_catalog_uzbek_texts() -> None:
    """The admin ``FIELD_MESSAGES`` and the catalog's ``field.<code>`` say the same thing in Uzbek."""
    for code, message in FIELD_MESSAGES.items():
        assert i18n.CATALOG[f"field.{code}"]["uz"] == message, code


def test_customer_endpoint_field_codes_have_their_keys() -> None:
    customer_codes = {
        "otp_invalid",
        "password_too_short",
        "password_too_common",
        "password_entirely_numeric",
        "password_too_similar",
        "password_unchanged",
        "password_incorrect",
        "terms_outdated",
        "push_token_missing",
        "latlng_pair",
        "address_incomplete",
    }
    assert customer_codes <= ENDPOINT_FIELD_CODES
    assert all(f"field.{code}" in i18n.CATALOG for code in customer_codes)


def test_t_renders_and_falls_back_to_uzbek() -> None:
    assert i18n.t("error.otp_locked", "ru", m=3) == "Попытки закончились. Повторите через 3 мин."
    assert i18n.t("error.otp_locked", "en", m=3) == "No attempts left. Try again in 3 min."
    assert i18n.t("error.not_found", "de") == i18n.t("error.not_found") == "Topilmadi."
    assert i18n.t("sms.otp", "uz", code="123456").startswith("Glow Derma: tasdiqlash kodingiz 123456.")
    with pytest.raises(KeyError, match="needs"):
        i18n.t("error.otp_locked", "uz")
    with pytest.raises(KeyError):
        i18n.t("error.invented_code", "uz")


@pytest.mark.parametrize(
    ("header", "customer_language", "expected"),
    [
        ("ru-RU", None, "ru"),
        ("en;q=0.5, uz", None, "uz"),
        ("en;q=0.5, ru;q=0.9", None, "ru"),
        ("fr, de;q=0.8", None, "uz"),
        ("fr, de;q=0.8", "en", "en"),
        ("", "ru", "ru"),
        ("", None, "uz"),
        ("uz;q=0, en", None, "en"),
        ("en-GB;q=abc, ru", None, "ru"),
        ("*", "en", "en"),
        ("EN", None, "en"),
    ],
)
def test_resolve_language_matrix(header: str, customer_language: str | None, expected: str) -> None:
    request = RequestFactory().get("/", HTTP_ACCEPT_LANGUAGE=header) if header else RequestFactory().get("/")
    customer = SimpleNamespace(language=customer_language) if customer_language else None
    assert i18n.resolve_language(request, customer) == expected


def test_resolve_language_ignores_an_unsupported_customer_language() -> None:
    assert i18n.resolve_language(RequestFactory().get("/"), SimpleNamespace(language="de")) == "uz"


def test_numbers_and_percents_follow_the_admin_rules() -> None:
    nbsp = " "
    assert i18n.format_number(784400, "uz") == f"784{nbsp}400"
    assert i18n.format_number(1234567, "ru") == f"1{nbsp}234{nbsp}567"
    assert i18n.format_number(-5680, "uz") == f"−5{nbsp}680"
    assert i18n.format_number(784400, "en") == "784,400"
    assert i18n.format_number(-5680, "en") == "-5,680"
    assert i18n.format_number(0) == "0"
    assert i18n.format_percent("1.00", "uz") == "1"
    assert i18n.format_percent("1.50", "ru") == "1,5"
    assert i18n.format_percent("0.57", "uz") == "0,57"
    assert i18n.format_percent("1.50", "en") == "1.5"
    assert i18n.format_percent("10", "en") == "10"
    assert i18n.format_percent("0.00", "uz") == "0"
    with pytest.raises(ValueError, match="percent"):
        i18n.format_percent("abc")
