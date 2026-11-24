"""The admin API in Uzbek, Russian and English (feat/admin-ux, DESIGN §G): the request language
(``core.api.language``), the catalog lookups (``core.uzsource``), the error envelope and the export renderer
in the request's language, the catalog sources, the marking ratchet (every staff-visible Uzbek literal is
``N_()``-marked, module by module) and the canary: a flow run in Russian stores and sends only Uzbek."""

from __future__ import annotations

import ast
import csv
import json
import re
import uuid
from collections.abc import Callable, Iterator
from io import BytesIO, StringIO
from ipaddress import ip_network
from pathlib import Path
from typing import Any

import pytest
from django.conf import settings
from django.http import Http404
from django.test import Client, RequestFactory, override_settings
from django.utils import translation
from openpyxl import load_workbook  # type: ignore[import-untyped]
from rest_framework import exceptions
from rest_framework.test import APIClient

from glowderma.accounts import tokens
from glowderma.accounts.errors import (
    PASSWORD_CHANGE_REQUIRED,
    MfaChallengeInvalid,
    MfaCodeInvalid,
    PasswordChangeRequired,
    PasswordTokenInvalid,
)
from glowderma.accounts.models import StaffUser
from glowderma.accounts.tests.factories import AuthSessionFactory, StaffUserFactory
from glowderma.appointments.models import AppointmentEvent
from glowderma.catalog.tests.factories import CategoryFactory, ProductFactory
from glowderma.commerce.models import OrderEvent
from glowderma.commerce.tests.support import pay, place
from glowderma.core import audit, console_catalog, labels, outbox, uzsource
from glowderma.core.api import exceptions as api_exceptions
from glowderma.core.api.exceptions import FIELD_MESSAGES, MESSAGES, ContractValidationError, field_error
from glowderma.core.api.fields import HTTPS_URL_INVALID, PercentField
from glowderma.core.api.language import admin_language, is_admin_path, request_admin_language
from glowderma.core.exports import attachment_header, file_stem, sheet_title
from glowderma.core.models import AuditEntry, IdempotencyRecord, OutboxMessage
from glowderma.customers.tests.helpers import make_customer
from glowderma.integrations.bts import fake as fake_bts
from glowderma.integrations.click import fake as fake_click
from glowderma.loyalty.models import LedgerEntry
from glowderma.notifications.models import Campaign, CampaignRecipient, InboxNotification

CYRILLIC = re.compile(r"[А-Яа-яЁё]")
ORDERS = "/api/v1/admin/orders/"
CATEGORIES = "/api/v1/admin/categories/"
# The representative messages of these tests with their translations (the committed catalogs carry them once
# ``console_messages`` has run; the tests do not wait for that).
TRANSLATIONS: dict[str, dict[str, str]] = {
    "Avtorizatsiya talab qilinadi.": {"en": "Please sign in.", "ru": "Требуется авторизация."},
    "Maydonlarni tekshiring.": {"en": "Please check the fields.", "ru": "Проверьте поля."},
    "Topilmadi.": {"en": "Not found.", "ru": "Не найдено."},
    "Ruxsat etilmagan qiymat.": {"en": "This value is not allowed.", "ru": "Недопустимое значение."},
    "Fayl juda katta.": {"en": "The file is too large.", "ru": "Файл слишком большой."},
    "Kamida {limit} ta belgi kiriting.": {
        "en": "Enter at least {limit} characters.",
        "ru": "Введите не менее {limit} символов.",
    },
    "{limit} belgidan oshmasin.": {
        "en": "No more than {limit} characters.",
        "ru": "Не более {limit} символов.",
    },
    "Qiymat {limit} dan oshmasin.": {
        "en": "The value must not exceed {limit}.",
        "ru": "Значение не должно превышать {limit}.",
    },
    "Qiymat %(min)s dan katta va %(max)s dan oshmasin.": {
        "en": "The value must be greater than %(min)s and not exceed %(max)s.",
        "ru": "Значение должно быть больше %(min)s и не превышать %(max)s.",
    },
    "Juda ko‘p urinish. %(n)s soniyadan so‘ng qayta urinib ko‘ring.": {
        "en": "Too many attempts. Try again in %(n)s s.",
        "ru": "Слишком много попыток. Повторите через %(n)s с.",
    },
    "Sababni kiriting.": {"en": "Enter a reason.", "ru": "Укажите причину."},
    "Kamida %(amount)s so‘m: %(status)s.": {
        "en": "At least %(amount)s UZS: %(status)s.",
        "ru": "Не менее %(amount)s сум: %(status)s.",
    },
    PASSWORD_CHANGE_REQUIRED: {"en": "Change your password first.", "ru": "Сначала смените пароль."},
    # export headers and name of the categories list (``Faol``, ``Ha`` / ``Yo‘q`` are in the catalog already)
    "Slug": {"en": "Slug", "ru": "Slug"},
    "Nomi": {"en": "Name", "ru": "Название"},
    "Tartib": {"en": "Position", "ru": "Порядок"},
    "Mahsulotlar": {"en": "Products", "ru": "Товары"},
    "Rasm": {"en": "Image", "ru": "Изображение"},
    "kategoriyalar": {"en": "categories", "ru": "категории"},
}


@pytest.fixture
def translations(monkeypatch: pytest.MonkeyPatch) -> Iterator[Callable[[dict[str, dict[str, str]]], None]]:
    """The committed catalogs plus test entries (``{uz: {en, ru}}``); :data:`TRANSLATIONS` from the start."""
    real = uzsource.catalog
    overlay: dict[str, dict[str, str]] = {language: {} for language in uzsource.TARGET_LANGUAGES}

    def catalog(language: str) -> dict[str, str]:
        if language not in uzsource.TARGET_LANGUAGES:
            return {}
        return {**real(language), **overlay[language]}

    def add(entries: dict[str, dict[str, str]]) -> None:
        for source, texts in entries.items():
            for language, text in texts.items():
                overlay[language][source] = text
        uzsource.templates.cache_clear()

    monkeypatch.setattr(uzsource, "catalog", catalog)
    add(TRANSLATIONS)
    yield add
    uzsource.templates.cache_clear()


@pytest.fixture
def fake_providers() -> Iterator[None]:
    fake_bts.reset()
    fake_click.reset()
    yield
    fake_bts.reset()
    fake_click.reset()


def internal_client() -> APIClient:
    client = APIClient()
    client.default_format = "json"
    client.credentials(HTTP_X_GD_INTERNAL_SECRET=settings.INTERNAL_API_SECRET)
    return client


def client_for(user: StaffUser) -> APIClient:
    access, _ = tokens.issue_access(AuthSessionFactory.create(staff=user))
    client = internal_client()
    client.credentials(
        HTTP_AUTHORIZATION=f"Bearer {access}", HTTP_X_GD_INTERNAL_SECRET=settings.INTERNAL_API_SECRET
    )
    return client


def idempotent(client: APIClient, path: str, body: Any, language: str) -> Any:
    return client.post(
        path, body, format="json", HTTP_IDEMPOTENCY_KEY=str(uuid.uuid4()), HTTP_ACCEPT_LANGUAGE=language
    )


def admin_request(language: str | None = None, path: str = ORDERS) -> Any:
    request = RequestFactory().get(path)
    if language is not None:
        request.META["HTTP_ACCEPT_LANGUAGE"] = language
    return request


# --- the request language ---------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "header,expected",
    [
        ("ru", "ru"),
        ("en-GB,en;q=0.9", "en"),
        ("ru-RU", "ru"),
        ("de, en;q=0.5, ru;q=0.8", "ru"),
        ("de-DE", "uz"),
        ("", "uz"),
        ("*", "uz"),
        ("ru;q=0, en;q=abc", "uz"),
        ("uz, ru", "uz"),
        ("x" * 5000, "uz"),
        ('ru"><script>', "uz"),
    ],
)
def test_admin_language_reads_accept_language_only(header: str, expected: str) -> None:
    assert admin_language(admin_request(header)) == expected


def test_admin_language_never_reads_the_account_or_a_cookie() -> None:
    request = admin_request()
    request.COOKIES["gd_admin_language"] = "ru"
    request.user = type("U", (), {"language": "ru", "is_authenticated": True})()
    assert admin_language(request) == "uz"


def test_only_admin_paths_take_the_request_language() -> None:
    assert is_admin_path("/api/v1/admin/") and is_admin_path("/api/v1/admin") and is_admin_path(ORDERS)
    assert not is_admin_path("/api/v1/administration/") and not is_admin_path("/api/v1/health/")
    assert request_admin_language(admin_request("ru")) == "ru"
    assert request_admin_language(admin_request("ru", "/api/v1/health/")) == "uz"
    assert request_admin_language(None) == "uz"


# --- the catalog lookups ----------------------------------------------------------------------------------


def test_n_is_a_no_op_and_tr_is_verbatim_in_uzbek(translations: Any) -> None:
    assert uzsource.N_("Topilmadi.") == "Topilmadi."
    assert translation.get_language() == "uz"
    assert uzsource.tr("Topilmadi.") == "Topilmadi."
    with translation.override(None):
        assert uzsource.tr("Topilmadi.") == "Topilmadi."
    with translation.override("ru"):
        assert uzsource.tr("Topilmadi.") == "Не найдено."
        assert uzsource.tr("Hech kim bilmaydigan matn.") == "Hech kim bilmaydigan matn."
        assert uzsource.tr("") == ""
    assert uzsource.tr("Topilmadi.", "en") == "Not found."
    assert uzsource.lookup("Topilmadi.", "uz") is None and uzsource.lookup("Topilmadi.", "de") is None


def test_labels_follow_the_active_language_only(translations: Any) -> None:
    assert labels.yes_no(True) == "Ha" and labels.active_label(False) == "Nofaol"
    assert labels.text(labels.ORDER_STATUS, "processing") == "Jarayonda"
    assert (
        labels.text(labels.ORDER_STATUS, "unknown", "-") == "-"
        and labels.text(labels.ORDER_STATUS, None) == ""
    )
    with translation.override("en"):
        assert (labels.yes_no(True), labels.yes_no(False)) == ("Yes", "No")
        assert labels.active_label(True) == "Active"
        assert labels.text(labels.ORDER_STATUS, "processing") == "Processing"
        assert labels.refund_state_label("succeeded", "click") == uzsource.tr(
            labels.REFUND_STATE["succeeded"]
        )


def test_the_catalog_holds_only_the_projects_own_messages() -> None:
    """``locale/ru`` also translates Unfold's and Django's English strings that our code never uses: they are
    never an Uzbek source text, so the admin lookups leave them out."""
    uzsource.clear_caches()
    english, russian = uzsource.catalog("en"), uzsource.catalog("ru")
    assert english and russian and set(russian) <= set(english)
    assert uzsource.catalog("uz") == {} and uzsource.catalog("de") == {}
    ours = {entry.msgid for entry in console_catalog.read_entries("en")}
    foreign = {entry.msgid for entry in console_catalog.read_entries("ru")} - ours
    assert foreign and not foreign & set(russian)


def test_an_exact_catalog_text_is_translated(translations: Any) -> None:
    assert uzsource.localize_admin_message("Topilmadi.", "ru") == "Не найдено."
    assert uzsource.localize_admin_message("Topilmadi.", "en") == "Not found."
    for language in ("uz", None, "de", ""):
        assert uzsource.localize_admin_message("Topilmadi.", language) == "Topilmadi."
    assert uzsource.localize_admin_message("Yangi, hali belgilanmagan xabar.", "ru") == (
        "Yangi, hali belgilanmagan xabar."
    )


def test_a_rendered_template_is_read_back_and_numbers_are_regrouped(translations: Any) -> None:
    localize = uzsource.localize_admin_message
    assert localize("Kamida 3 ta belgi kiriting.", "en") == "Enter at least 3 characters."
    assert localize("500 belgidan oshmasin.", "ru") == "Не более 500 символов."
    assert localize("Juda ko‘p urinish. 42 soniyadan so‘ng qayta urinib ko‘ring.", "en") == (
        "Too many attempts. Try again in 42 s."
    )
    amount = "Kamida 12\u00a0000 so‘m: Jarayonda."
    assert localize(amount, "en") == "At least 12,000 UZS: Processing."  # the label value is translated too
    assert localize(amount, "ru") == "Не менее 12\u00a0000 сум: В обработке."
    assert localize("Kamida \u22121\u00a0250\u00a0000 so‘m: x.", "en") == "At least -1,250,000 UZS: x."


def test_the_most_specific_template_wins(translations: Any) -> None:
    """ "Qiymat 0 dan katta va 100 dan oshmasin." also reads as "Qiymat {limit} dan oshmasin." with a long
    limit: the template with more literal text is tried first."""
    assert uzsource.localize_admin_message("Qiymat 0 dan katta va 100 dan oshmasin.", "en") == (
        "The value must be greater than 0 and not exceed 100."
    )
    assert (
        uzsource.localize_admin_message("Qiymat 100 dan oshmasin.", "en") == "The value must not exceed 100."
    )


def test_values_are_inserted_verbatim_never_expanded(translations: Any) -> None:
    hostile = "Kamida %(n)s{limit}{{x}} ta belgi kiriting."
    assert uzsource.localize_admin_message(hostile, "en") == "Enter at least %(n)s{limit}{{x}} characters."
    long_value = "Kamida " + "9" * (uzsource.MAX_MATCHED_LENGTH + 10) + " ta belgi kiriting."
    assert uzsource.localize_admin_message(long_value, "en") == long_value


def test_placeholder_only_templates_never_match(translations: Any) -> None:
    translations({"%(a)s: %(b)s": {"en": "%(b)s / %(a)s", "ru": "%(b)s / %(a)s"}})
    assert uzsource.localize_admin_message("Ism: Ali", "en") == "Ism: Ali"
    assert all(template.msgid != "%(a)s: %(b)s" for template in uzsource.templates("en"))


def test_a_translation_with_an_unknown_placeholder_is_not_used(translations: Any) -> None:
    translations({"Qator %(n)s ta.": {"en": "Rows %(n)s %(extra)s.", "ru": "Строк %(n)s."}})
    assert uzsource.localize_admin_message("Qator 5 ta.", "en") == "Qator 5 ta."
    assert uzsource.localize_admin_message("Qator 5 ta.", "ru") == "Строк 5."


def test_placeholders_of_both_styles() -> None:
    assert uzsource.placeholders("A %(n)s b {limit} c %% {{d}}") == {"n", "limit"}
    assert uzsource.placeholders("100%% tayyor") == set()


# --- the error envelope -----------------------------------------------------------------------------------


def handle(exc: Exception, language: str | None, path: str = ORDERS) -> Any:
    response = api_exceptions.envelope_exception_handler(exc, {"request": admin_request(language, path)})
    return response.data


def test_the_envelope_renders_message_and_field_messages(translations: Any) -> None:
    exc = ContractValidationError(
        {"reason": [field_error("min_length", limit=3)], "page_size": [field_error("invalid_choice")]},
        message="Sababni kiriting.",
    )
    body = handle(exc, "en")
    assert body["code"] == "validation_error" and body["message"] == "Enter a reason."
    assert body["errors"] == [
        {"field": "reason", "code": "min_length", "message": "Enter at least 3 characters."},
        {"field": "page_size", "code": "invalid_choice", "message": "This value is not allowed."},
    ]
    assert set(body) == {"code", "message", "errors", "request_id"}
    uzbek = handle(exc, None)
    assert uzbek["message"] == "Sababni kiriting."
    assert uzbek["errors"][0]["message"] == "Kamida 3 ta belgi kiriting."
    assert handle(exc, "ru")["errors"][0]["message"] == "Введите не менее 3 символов."


def test_the_envelope_translates_drf_and_project_errors(translations: Any) -> None:
    assert handle(exceptions.NotAuthenticated(), "ru")["message"] == "Требуется авторизация."
    assert handle(exceptions.NotFound(), "en")["message"] == "Not found."
    throttled = handle(exceptions.Throttled(wait=42), "en")
    assert (throttled["code"], throttled["message"]) == ("throttled", "Too many attempts. Try again in 42 s.")
    # a code another module added (accounts.errors, feat/admin-ux) is covered once its text is in the catalog
    pending = handle(PasswordChangeRequired(), "ru")
    assert (pending["code"], pending["message"]) == ("password_change_required", "Сначала смените пароль.")
    stock = exceptions.ValidationError({"name": ["This field is required."]})
    assert handle(stock, "en")["errors"] == [
        {"field": "name", "code": "invalid", "message": "This field is required."}
    ]


@pytest.mark.parametrize(
    "error,code",
    [
        (PasswordTokenInvalid, "token_invalid"),
        (MfaCodeInvalid, "mfa_code_invalid"),
        (MfaChallengeInvalid, "mfa_challenge_invalid"),
    ],
)
def test_the_invite_and_mfa_errors_follow_the_language(
    translations: Any, error: type[exceptions.APIException], code: str
) -> None:
    source = str(error.default_detail)
    translations({source: {"en": f"EN {code}", "ru": f"РУ {code}"}})
    assert handle(error(), "en") == {"code": code, "message": f"EN {code}", "errors": [], "request_id": ""}
    assert handle(error(), "ru")["message"] == f"РУ {code}"
    # the same code, Uzbek text, without a header (one uniform answer: nothing else leaks)
    assert (handle(error(), None)["code"], handle(error(), None)["message"]) == (code, source)


def test_the_envelope_stays_uzbek_outside_the_admin_api(translations: Any) -> None:
    assert handle(exceptions.NotFound(), "ru", "/api/v1/health/")["message"] == "Topilmadi."
    assert handle(exceptions.NotFound(), "de")["message"] == "Topilmadi."


def test_the_admin_not_found_and_server_error_pages_follow_the_language(translations: Any) -> None:
    page = api_exceptions.not_found_view(admin_request("ru", "/api/v1/admin/nope/"), Http404())
    assert json.loads(page.content) == {
        "code": "not_found",
        "message": "Не найдено.",
        "errors": [],
        "request_id": "",
    }
    error = api_exceptions.server_error_view(admin_request("en", "/api/v1/admin/orders/"))
    assert json.loads(error.content)["message"] == uzsource.localize_admin_message(
        MESSAGES["server_error"], "en"
    )
    health = api_exceptions.not_found_view(admin_request("ru", "/api/v1/health/nope/"), Http404())
    assert json.loads(health.content)["message"] == "Topilmadi."


@pytest.mark.django_db
def test_admin_answers_carry_content_language_and_vary(translations: Any) -> None:
    client = internal_client()
    anonymous = client.get(ORDERS, HTTP_ACCEPT_LANGUAGE="ru")
    assert anonymous.status_code == 401 and anonymous.json()["message"] == "Требуется авторизация."
    assert anonymous["Content-Language"] == "ru" and "Accept-Language" in anonymous["Vary"]
    uzbek = client.get(ORDERS)
    assert uzbek.json()["message"] == "Avtorizatsiya talab qilinadi." and uzbek["Content-Language"] == "uz"
    unknown = client.get(ORDERS, HTTP_ACCEPT_LANGUAGE="xx-evil")
    assert unknown["Content-Language"] == "uz"
    health = client.get("/api/v1/health/", HTTP_ACCEPT_LANGUAGE="ru")
    assert health["Content-Language"] == "uz" and "Accept-Language" not in health.get("Vary", "")


@pytest.mark.django_db
def test_representative_endpoints_in_russian_and_english(
    translations: Any, staff_client: APIClient, fake_providers: None
) -> None:
    order = place()
    path = f"{ORDERS}{order.pk}/cancel/"
    short = idempotent(staff_client, path, {"reason": "  ab  "}, "en")
    assert short.status_code == 400 and short["Content-Language"] == "en"
    assert short.json()["message"] == "Enter a reason."
    assert short.json()["errors"] == [
        {"field": "reason", "code": "min_length", "message": "Enter at least 3 characters."}
    ]
    long = idempotent(staff_client, path, {"reason": "x" * 501}, "ru")
    assert long.json()["errors"][0]["message"] == "Не более 500 символов."
    paging = staff_client.get(ORDERS, {"page_size": 7}, HTTP_ACCEPT_LANGUAGE="ru")
    assert paging.json()["errors"] == [
        {"field": "page_size", "code": "invalid_choice", "message": "Недопустимое значение."}
    ]
    missing = staff_client.get(f"{ORDERS}999999/", HTTP_ACCEPT_LANGUAGE="en")
    assert missing.status_code == 404 and missing.json()["message"] == "Not found."
    # the same request in Uzbek is byte for byte the source text
    uzbek = idempotent(staff_client, path, {"reason": "  ab  "}, "uz")
    assert uzbek.json()["message"] == "Sababni kiriting." and uzbek["Content-Language"] == "uz"


@pytest.mark.django_db
def test_a_too_large_admin_request_is_refused_in_its_language(translations: Any) -> None:
    response = internal_client().post(
        ORDERS,
        data=b"x",
        content_type="application/json",
        CONTENT_LENGTH=str(7 * 1024 * 1024),
        HTTP_ACCEPT_LANGUAGE="en",
    )
    assert response.status_code == 413 and response.json()["message"] == "The file is too large."
    customer = internal_client().post(
        "/api/v1/health/",
        data=b"x",
        content_type="application/json",
        CONTENT_LENGTH=str(7 * 1024 * 1024),
        HTTP_ACCEPT_LANGUAGE="en",
    )
    assert customer.json()["message"] == "Fayl juda katta."


@override_settings(ADMIN_API_ALLOWED_NETS=[ip_network("10.0.0.0/8")])
def test_the_admin_network_gate_answers_in_the_request_language(translations: Any) -> None:
    translations({"Ruxsat yo‘q.": {"en": "You do not have permission to do this.", "ru": "Нет доступа."}})
    response = Client().get("/api/v1/admin/test/", REMOTE_ADDR="8.8.8.8", HTTP_ACCEPT_LANGUAGE="ru")
    assert response.status_code == 403 and response.json()["message"] == "Нет доступа."
    assert response["Content-Language"] == "ru"
    schema = Client().get("/api/v1/schema/", REMOTE_ADDR="8.8.8.8", HTTP_ACCEPT_LANGUAGE="ru")
    assert schema.status_code == 403 and schema.json()["message"] == "Ruxsat yo‘q."


@pytest.mark.django_db
def test_customer_errors_keep_their_own_catalog(translations: Any) -> None:
    response = APIClient().get("/api/v1/me/", HTTP_ACCEPT_LANGUAGE="ru")
    assert response.status_code == 400 and "details" in response.json()  # the customer envelope
    assert response["Content-Language"] == "ru"


def test_core_field_texts_are_marked_and_unchanged_in_uzbek() -> None:
    assert HTTPS_URL_INVALID == "Havolani https:// bilan to‘liq kiriting."
    assert PercentField(exclusive_min=0, inclusive_max=100)._range_message() == (
        "Qiymat 0 dan katta va 100 dan oshmasin."
    )
    assert PercentField(exclusive_min=0)._range_message() == "Qiymat 0 dan katta bo‘lsin."
    assert PercentField(inclusive_max=100)._range_message() == "Qiymat 100 dan oshmasin."
    assert PercentField()._range_message() == FIELD_MESSAGES["invalid"]


# --- exports ----------------------------------------------------------------------------------------------


@pytest.mark.django_db
def test_export_headers_name_and_values_follow_the_language(
    translations: Any, staff_client: APIClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    serums = CategoryFactory.create(slug="serums", name_uz="Serumlar", position=0)
    ProductFactory.create(category=serums)
    CategoryFactory.create(slug="toners", name_uz="Tonerlar", position=1, is_active=False)
    recorded: list[str | None] = []
    real_record = audit.record

    def record(**kwargs: Any) -> AuditEntry:
        recorded.append(translation.get_language())
        return real_record(**kwargs)

    monkeypatch.setattr(audit, "record", record)
    english = staff_client.get(CATEGORIES, {"export": "csv"}, HTTP_ACCEPT_LANGUAGE="en")
    assert english.status_code == 200 and english["Content-Language"] == "en"
    assert re.fullmatch(r'attachment; filename="categories-\d{8}-\d{4}\.csv"', english["Content-Disposition"])
    rows = list(csv.reader(StringIO(english.content.decode("utf-8-sig")), delimiter=";"))
    assert rows == [
        ["Slug", "Name", "Position", "Active", "Products", "Image"],
        ["serums", "Serumlar", "0", "Yes", "1", ""],  # data (the name) is never translated
        ["toners", "Tonerlar", "1", "No", "0", ""],
    ]
    russian = staff_client.get(CATEGORIES, {"export": "xlsx"}, HTTP_ACCEPT_LANGUAGE="ru")
    disposition = russian["Content-Disposition"]
    assert re.fullmatch(
        r"attachment; filename=\"kategorii-\d{8}-\d{4}\.xlsx\"; "
        r"filename\*=UTF-8''%D0%BA%D0%B0%D1%82%D0%B5%D0%B3%D0%BE%D1%80%D0%B8%D0%B8-\d{8}-\d{4}\.xlsx",
        disposition,
    ), disposition
    sheet = load_workbook(BytesIO(russian.content), read_only=True).worksheets[0]
    assert sheet.title == "категории"
    assert [cell.value for cell in next(sheet.iter_rows(max_row=1))] == [
        "Slug",
        "Название",
        "Порядок",
        "Активна",  # the categories export's own rendering (review F2)
        "Товары",
        "Изображение",
    ]
    uzbek = staff_client.get(CATEGORIES, {"export": "csv"})
    assert uzbek["Content-Language"] == "uz" and uzbek["Content-Disposition"].startswith(
        'attachment; filename="kategoriyalar-'
    )
    assert (
        uzbek.content.decode("utf-8-sig").split("\r\n")[0]
        == '"Slug";"Nomi";"Tartib";"Faol";"Mahsulotlar";"Rasm"'
    )
    # the audit rows of the downloads are written in Uzbek, outside the export's language
    assert recorded == ["uz", "uz", "uz"]
    stored = json.dumps(list(AuditEntry.objects.values()), default=str, ensure_ascii=False)
    assert not CYRILLIC.search(stored) and "Position" not in stored
    assert translation.get_language() == "uz"


def test_export_name_helpers_are_safe() -> None:
    assert sheet_title("a/b\\c?d*e[f]g:h", "x") == "abcdefgh"
    assert sheet_title("ж" * 40, "x") == "ж" * 31 and sheet_title("[]:", "fallback") == "fallback"
    assert file_stem('mahsulotlar"; x=y/../\r\n', "z") == "mahsulotlar-x-y"
    assert file_stem("журнал действий", "z") == "журнал-действий" and file_stem("\"';", "z") == "z"
    assert attachment_header("products-1.csv", "x.csv") == 'attachment; filename="products-1.csv"'
    assert attachment_header("товары-1.csv", "mahsulotlar-1.csv") == (
        'attachment; filename="mahsulotlar-1.csv"; '
        "filename*=UTF-8''%D1%82%D0%BE%D0%B2%D0%B0%D1%80%D1%8B-1.csv"
    )


# --- the catalog sources ----------------------------------------------------------------------------------


def test_the_collector_reads_n_markers() -> None:
    source = (
        "from glowderma.core.uzsource import N_\n"
        'A = N_("Topilmadi.")\n'
        'def f(n):\n    return N_("Kamida %(n)s ta.") % {"n": n}, uzsource.N_("Ikki")\n'
    )
    assert set(console_catalog.python_messages(source)) == {
        console_catalog.Message("Topilmadi."),
        console_catalog.Message("Kamida %(n)s ta."),
        console_catalog.Message("Ikki"),
    }


def test_the_generic_messages_and_export_headers_are_catalog_sources() -> None:
    messages = console_catalog.message_texts()
    assert set(messages) == {*MESSAGES.values(), *FIELD_MESSAGES.values()}
    assert messages["Topilmadi."].startswith("glowderma.core.api.exceptions.")
    headers = {
        text for name, value in vars(labels).items() if name.endswith("_EXPORT_HEADERS") for text in value
    }
    assert headers and headers <= set(console_catalog.dynamic_texts())


def admin_sources() -> set[str]:
    """Every ``N_`` text of the admin API and the generic messages: what this file holds to the catalog."""
    found: set[str] = set(console_catalog.message_texts())
    for path in console_catalog.python_files():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and _name(node.func) == "N_" and node.args:
                text = node.args[0]
                if isinstance(text, ast.Constant) and isinstance(text.value, str):
                    found.add(text.value)
    return found


def test_core_marks_its_own_texts() -> None:
    sources = admin_sources()
    for text in (
        HTTPS_URL_INVALID,
        "Qiymat %(min)s dan katta va %(max)s dan oshmasin.",
        "Juda ko‘p urinish. %(n)s soniyadan so‘ng qayta urinib ko‘ring.",
        "Juda ko‘p qator. Filtrni toraytiring.",
        "Telefon raqamini to‘g‘ri kiriting.",
        "Bu slug band.",
        *MESSAGES.values(),
        *FIELD_MESSAGES.values(),
    ):
        assert text in sources, text


# The committed translation of an admin text may equal its source only for these (names and codes).
SAME_IN_EVERY_LANGUAGE = frozenset({"ID", "IP", "BTS", "Slug", "Billz ID", "№"})


@pytest.mark.parametrize("language", uzsource.TARGET_LANGUAGES)
def test_committed_translations_of_admin_texts_differ_and_keep_placeholders(language: str) -> None:
    """Completeness is ``test_console_languages``' job (a missing entry fails there); this holds every entry
    the catalog has for an admin text to a real translation."""
    entries = {
        entry.msgid: entry for entry in console_catalog.read_entries(language) if entry.msgctxt is None
    }
    problems = []
    for text in sorted(admin_sources()):
        entry = entries.get(text)
        translated = entry.msgstr.get(0, "") if entry is not None else ""
        if not translated:
            continue
        if translated == text and text not in SAME_IN_EVERY_LANGUAGE:
            problems.append(f"not translated: {text!r}")
        if uzsource.placeholders(translated) != uzsource.placeholders(text):
            problems.append(f"placeholders differ: {text!r} -> {translated!r}")
        if language == "ru" and text not in SAME_IN_EVERY_LANGUAGE and not CYRILLIC.search(translated):
            problems.append(f"no Cyrillic: {text!r} -> {translated!r}")
        if language == "en" and (CYRILLIC.search(translated) or re.search("[‘ʻ]", translated)):
            problems.append(f"not English: {text!r} -> {translated!r}")
    assert problems == []


# --- the marking ratchet ----------------------------------------------------------------------------------
#
# Every staff-visible Uzbek literal of the admin API is ``N_()``-marked (or marked for the console with
# ``gettext`` / ``gettext_lazy``): the error envelope can only translate what the catalog knows. The scan
# looks at the literals that become an API message — in a ``raise`` (but of a builtin programming error), in a
# ``field_error`` / ``ErrorDetail`` / ``*ValidationError`` call, a ``message=`` / ``detail=`` /
# ``error_messages=`` argument, a ``{"message": …}`` entry, a ``*MESSAGE(S)*`` / ``default_detail`` /
# ``default_error_messages`` assignment, and a module constant such a place names — and flags the Uzbek ones
# that are not marked. ``NOT_YET_MARKED`` lists the modules the marking pass has not done yet: it only shrinks
# (a listed module without a finding fails, so its entry goes), and it ends empty.

NOT_YET_MARKED = frozenset[str]()
# Never scanned: what customers, POS terminals and providers get (rendered from ``core.i18n`` keys or in the
# provider's own protocol), command-line output, and the provider adapters (fakes mimic the provider's text).
EXEMPT = re.compile(
    r"/(tests|migrations|management)/|/api/(customer|pos|webhooks|dev)/"
    # a provider's adapter modules, also once they are packages (billz/client/, billz/live/)
    r"|^glowderma/integrations/[a-z_]+/(fake|live|protocol|client|ports)(/[a-z_]+)?\.py$"
)
MARKERS = frozenset(
    {
        "N_",
        "_",
        "gettext",
        "gettext_lazy",
        "gettext_noop",
        "ngettext",
        "ngettext_lazy",
        "pgettext",
        "pgettext_lazy",
    }
)
BUILTIN_ERRORS = frozenset(
    {
        "ValueError",
        "TypeError",
        "RuntimeError",
        "KeyError",
        "LookupError",
        "IndexError",
        "AttributeError",
        "AssertionError",
        "NotImplementedError",
        "PermissionError",
        "OSError",
        "Exception",
        "SystemExit",
        "ImproperlyConfigured",
        "CommandError",
    }
)
ERROR_CALLS = frozenset({"field_error", "ErrorDetail", "ContractValidationError"})
MESSAGE_KEYWORDS = frozenset({"message", "detail", "error_messages", "default_detail"})
METADATA_KEYWORDS = frozenset({"verbose_name", "verbose_name_plural", "help_text"})
MESSAGE_NAME = re.compile(r"(^|_)MESSAGES?($|_)|^default_detail$|^default_error_messages$|^message$", re.I)
# An Uzbek text: o‘ / g‘ / the tutuq belgisi / guillemets, or one of the words API messages are made of.
UZBEK = re.compile(
    r"[‘ʻ’«»]|\b(?:va|yoki|bu|ta|uchun|bilan|emas|kerak|mumkin|topilmadi|qiymat\w*|maydon\w*|qayta|kiriting"
    r"|kiring|tanlang|kamida|oshmasin|allaqachon|boshqa|faqat|avval|holat\w*|buyurtma\w*|mijoz\w*|mahsulot\w*"
    r"|xodim\w*|parol\w*|kod\w*|sana|vaqt\w*|xato\w*|bekor|nomi|ruxsat|joriy|yangi|yaroqsiz|muddat\w*|sabab\w*"
    r"|summa\w*|balans\w*|qabul\w*|mutaxassis\w*|xizmat\w*|rasm\w*|havola\w*|tekshiring|ishlamaydi|tugadi"
    r"|kirildi|kutilmagan|bajarilmoqda|sarlavha\w*|javob|bermadi|oshmadi|hisob\w*|tanlangan|ochiq|yopiq"
    r"|qoldiq|narx\w*)\b",
    re.IGNORECASE,
)


def _name(node: ast.expr) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return ""


def _contexts(tree: ast.AST) -> Iterator[ast.AST]:
    """The expressions whose literals become an admin API message."""
    for node in ast.walk(tree):
        if isinstance(node, ast.Raise) and node.exc is not None:
            raised = node.exc.func if isinstance(node.exc, ast.Call) else node.exc
            if _name(raised) not in BUILTIN_ERRORS:
                yield node.exc
        elif isinstance(node, ast.Call) and (
            _name(node.func) in ERROR_CALLS or _name(node.func).endswith("ValidationError")
        ):
            yield node
        elif isinstance(node, ast.keyword) and node.arg in MESSAGE_KEYWORDS:
            yield node.value
        elif isinstance(node, ast.Dict):
            for key, value in zip(node.keys, node.values, strict=True):
                if isinstance(key, ast.Constant) and key.value in ("message", "detail"):
                    yield value
        elif isinstance(node, (ast.Assign, ast.AnnAssign)) and node.value is not None:
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            if any(MESSAGE_NAME.search(_name(target)) for target in targets):
                yield node.value


def referenced_names(tree: ast.AST) -> set[str]:
    """The names (and attribute names) the message places of a module use: constants that hold a message."""
    names: set[str] = set()
    for context in _contexts(tree):
        for node in ast.walk(context):
            if isinstance(node, ast.Name):
                names.add(node.id)
            elif isinstance(node, ast.Attribute):
                names.add(node.attr)
    return names


def unmarked_texts(tree: ast.Module, constants: set[str]) -> list[tuple[int, str]]:
    """The unmarked Uzbek literals of a module's message places and of its module constants in
    ``constants``, as ``(line, text)``."""
    skipped: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and _name(node.func) in MARKERS:
            for argument in node.args[:3]:
                skipped.update(id(inner) for inner in ast.walk(argument))
        elif isinstance(node, ast.keyword) and node.arg in METADATA_KEYWORDS:
            skipped.update(id(inner) for inner in ast.walk(node.value))
    roots: list[ast.AST] = list(_contexts(tree))
    for statement in tree.body:
        if isinstance(statement, (ast.Assign, ast.AnnAssign)) and statement.value is not None:
            targets = statement.targets if isinstance(statement, ast.Assign) else [statement.target]
            if any(
                isinstance(target, ast.Name) and target.id.isupper() and target.id in constants
                for target in targets
            ):
                roots.append(statement.value)
    found: set[tuple[int, str]] = set()
    for root in roots:
        for node in ast.walk(root):
            if (
                isinstance(node, ast.Constant)
                and isinstance(node.value, str)
                and id(node) not in skipped
                and UZBEK.search(node.value)
            ):
                found.add((node.lineno, node.value))
    return sorted(found)


def scan(sources: dict[str, str]) -> dict[str, list[tuple[int, str]]]:
    trees = {path: ast.parse(text) for path, text in sources.items()}
    constants: set[str] = set()
    for tree in trees.values():
        constants |= referenced_names(tree)
    return {path: found for path, tree in trees.items() if (found := unmarked_texts(tree, constants))}


def project_sources() -> dict[str, str]:
    root = Path(settings.BASE_DIR)
    sources = {}
    for path in sorted((root / "glowderma").rglob("*.py")):
        relative = path.relative_to(root).as_posix()
        if "__pycache__" not in relative and not EXEMPT.search(relative):
            sources[relative] = path.read_text(encoding="utf-8")
    return sources


def test_every_staff_visible_uzbek_text_is_marked() -> None:
    findings = scan(project_sources())
    unexpected = [
        f"{path}:{line}: {text!r}"
        for path, found in sorted(findings.items())
        if path not in NOT_YET_MARKED
        for line, text in found
    ]
    assert unexpected == [], "mark these with core.uzsource.N_ (see AGENTS.md): " + "\n".join(unexpected)


def test_the_not_yet_marked_list_only_shrinks() -> None:
    findings = scan(project_sources())
    done = sorted(path for path in NOT_YET_MARKED if path not in findings)
    assert done == [], f"fully marked now: remove from NOT_YET_MARKED: {done}"


def test_the_scan_sees_every_kind_of_message_place() -> None:
    source = """
from glowderma.core.uzsource import N_
STATE_MESSAGE = "Holat noto‘g‘ri."
LIMIT = "Kamida bitta daraja kerak."
NAMED = N_("Bu nom band.")
class Gone(APIException):
    default_detail = "Mijoz topilmadi."
class Field(serializers.CharField):
    default_error_messages = {"invalid": "Noto‘g‘ri telefon.", "blank": N_("Maydonni to‘ldiring.")}
class Model(models.Model):
    message = models.TextField(verbose_name="xato matni")
def view(value):
    if value:
        raise Conflict(f"Buyurtma {value} bekor qilingan.")
    if value is None:
        raise ValidationError({"x": [field_error("invalid", LIMIT)]})
    if value == 0:
        raise ValueError("Bu holat bo‘lmasligi kerak")  # a programming error, never a message
    if value == 1:
        raise Conflict(N_("Kamida %(n)s ta.") % {"n": value})
    return {"message": "Yangi parol kiriting.", "other": "Mijoz ismi"}
"""
    found = {text for _line, text in scan({"m.py": source})["m.py"]}
    assert found == {
        "Holat noto‘g‘ri.",
        "Kamida bitta daraja kerak.",
        "Mijoz topilmadi.",
        "Noto‘g‘ri telefon.",
        "Buyurtma ",
        " bekor qilingan.",
        "Yangi parol kiriting.",
    }


def test_exempt_paths() -> None:
    assert EXEMPT.search("glowderma/commerce/api/customer/views.py")
    assert EXEMPT.search("glowderma/integrations/payme/protocol.py")
    assert EXEMPT.search("glowderma/integrations/billz/client/http_client.py")
    assert not EXEMPT.search("glowderma/integrations/services/jobs.py")
    assert not EXEMPT.search("glowderma/integrations/services.py")
    assert not EXEMPT.search("glowderma/commerce/api/admin/views.py")


# --- the canary: a flow in Russian stores and sends only Uzbek --------------------------------------------

STORED = (
    AuditEntry,
    OutboxMessage,
    OrderEvent,
    AppointmentEvent,
    LedgerEntry,
    Campaign,
    CampaignRecipient,
    InboxNotification,
    IdempotencyRecord,
)


def cyrillic_rows() -> dict[str, list[Any]]:
    found: dict[str, list[Any]] = {}
    for model in STORED:
        rows = [
            row
            for row in model.objects.values()
            if CYRILLIC.search(json.dumps(row, default=str, ensure_ascii=False))
        ]
        if rows:
            found[model._meta.label] = rows
    return found


@pytest.mark.django_db
def test_flows_in_russian_store_and_send_only_uzbek(
    monkeypatch: pytest.MonkeyPatch, staff_client: APIClient, fake_providers: None
) -> None:
    """Every Uzbek text the edge renders in Russian becomes Cyrillic here (a catalog that knows everything);
    after a set of admin flows sent with ``Accept-Language: ru`` the stored rows hold no Cyrillic, and every
    audit row and outbox message was written while Uzbek was the active language."""
    assert cyrillic_rows() == {}
    monkeypatch.setattr(
        uzsource,
        "lookup",
        lambda text, language, context=None: f"Перевод «{text}»" if language == "ru" else None,
    )
    languages: list[str | None] = []
    real_record, real_enqueue = audit.record, outbox.enqueue

    def record(**kwargs: Any) -> AuditEntry:
        languages.append(translation.get_language())
        return real_record(**kwargs)

    def enqueue(*args: Any, **kwargs: Any) -> OutboxMessage:
        languages.append(translation.get_language())
        return real_enqueue(*args, **kwargs)

    monkeypatch.setattr(audit, "record", record)
    monkeypatch.setattr(outbox, "enqueue", enqueue)
    answers: list[Any] = []
    # orders: a refused cancel, a cancel, a transition of a paid pickup order
    order = place()
    answers.append(idempotent(staff_client, f"{ORDERS}{order.pk}/cancel/", {"reason": "ab"}, "ru"))
    answers.append(
        idempotent(staff_client, f"{ORDERS}{order.pk}/cancel/", {"reason": "Mijoz so‘rovi bilan"}, "ru")
    )
    answers.append(idempotent(staff_client, f"{ORDERS}{order.pk}/cancel/", {"reason": "Yana bir bor"}, "ru"))
    pickup = pay(place(fulfilment="pickup"))
    answers.append(
        idempotent(staff_client, f"{ORDERS}{pickup.pk}/transition/", {"status": "delivered"}, "ru")
    )
    answers.append(
        idempotent(staff_client, f"{ORDERS}{pickup.pk}/transition/", {"status": "processing"}, "ru")
    )
    # a cashback adjustment, refused and done
    customer = make_customer("Malika Karimova")
    adjust = f"/api/v1/admin/customers/{customer.pk}/cashback-adjustments/"
    answers.append(idempotent(staff_client, adjust, {"direction": "add", "amount": 0, "reason": "x"}, "ru"))
    answers.append(
        idempotent(staff_client, adjust, {"direction": "add", "amount": 2_950, "reason": "Chek S-1"}, "ru")
    )
    # staff management: a new account (audit + its e-mail when asked for), and an export download
    manager = client_for(StaffUserFactory.create(is_manager=True))
    body = {"email": "nodira@glowderma.uz", "full_name": "Nodira Rahimova", "is_manager": False}
    answers.append(idempotent(manager, "/api/v1/admin/staff/", body, "ru"))
    answers.append(idempotent(manager, "/api/v1/admin/staff/", {"email": "bad"}, "ru"))
    CategoryFactory.create(slug="serums", name_uz="Serumlar")
    answers.append(staff_client.get(CATEGORIES, {"export": "csv"}, HTTP_ACCEPT_LANGUAGE="ru"))

    statuses = [answer.status_code for answer in answers]
    assert statuses == [400, 200, 409, 200, 409, 400, 201, 201, 400, 200], statuses
    refused = [answer.json() for answer in answers if answer.status_code >= 400]
    assert all(CYRILLIC.search(body["message"]) for body in refused), refused  # the edge did translate
    assert CYRILLIC.search(answers[-1].content.decode("utf-8-sig"))  # and so did the export
    assert languages and set(languages) == {"uz"}
    assert AuditEntry.objects.count() >= 5 and OrderEvent.objects.exists() and LedgerEntry.objects.exists()
    assert cyrillic_rows() == {}
