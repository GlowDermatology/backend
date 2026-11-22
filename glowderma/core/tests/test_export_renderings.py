"""Export texts as the committed catalogs render them (review F2), without the test overlay of
``test_admin_api_languages.py``: a Russian file name keeps an ASCII transliteration as its ``filename``
fallback, and a header or value that reads differently on one list ("Nomi" is a thing's name there, not a
person's; Russian adjectives agree with the list's noun) uses its ``export <resource>`` rendering.
"""

from __future__ import annotations

import csv
import re
from io import StringIO
from typing import Any

import pytest
from django.utils import translation
from rest_framework.test import APIClient

from glowderma.catalog.tests.factories import CategoryFactory, ProductFactory
from glowderma.core import console_catalog, labels, uzsource
from glowderma.core.exports import ExportMixin, ascii_stem, export_context

CATEGORIES = "/api/v1/admin/categories/"
PRODUCTS = "/api/v1/admin/products/"


def export_views() -> list[type[ExportMixin]]:
    from django.urls import get_resolver

    found: list[type[ExportMixin]] = []

    def walk(patterns: Any) -> None:
        for pattern in patterns:
            if hasattr(pattern, "url_patterns"):
                walk(pattern.url_patterns)
                continue
            view = getattr(pattern.callback, "cls", None)
            if isinstance(view, type) and issubclass(view, ExportMixin) and view not in found:
                found.append(view)

    walk(get_resolver().url_patterns)
    return found


def test_a_russian_stem_falls_back_to_its_transliteration() -> None:
    assert ascii_stem("товары", "mahsulotlar") == "tovary"
    assert ascii_stem("журнал-действий", "harakatlar-jurnali") == "zhurnal-deystviy"
    assert ascii_stem("Платежи", "tolovlar") == "Platezhi"
    assert ascii_stem("щука съёмка", "x") == "shchuka-semka"
    assert ascii_stem("产品", "mahsulotlar") == "mahsulotlar"  # no rule: the Uzbek stem
    assert ascii_stem("", "mahsulotlar") == "mahsulotlar"


def test_every_export_name_has_an_ascii_russian_fallback() -> None:
    views = export_views()
    assert len(views) >= 11  # noqa: PLR2004 - the services catalogue export is gone (per-specialist services)
    for view in views:
        russian = uzsource.tr(view.export_name, "ru")
        assert russian != view.export_name, view
        stem = ascii_stem(russian, "")
        assert stem and stem.isascii() and stem != view.export_name, (view, russian)


@pytest.mark.django_db
def test_a_russian_export_is_named_in_russian_with_a_transliterated_fallback(staff_client: APIClient) -> None:
    ProductFactory.create()
    russian = staff_client.get(PRODUCTS, {"export": "csv"}, HTTP_ACCEPT_LANGUAGE="ru")
    assert russian.status_code == 200
    assert re.fullmatch(
        r"attachment; filename=\"tovary-\d{8}-\d{4}\.csv\"; "
        r"filename\*=UTF-8''%D1%82%D0%BE%D0%B2%D0%B0%D1%80%D1%8B-\d{8}-\d{4}\.csv",
        russian["Content-Disposition"],
    ), russian["Content-Disposition"]
    header = next(csv.reader(StringIO(russian.content.decode("utf-8-sig")), delimiter=";"))
    assert header[3] == "Название"
    uzbek = staff_client.get(PRODUCTS, {"export": "csv"})
    assert re.fullmatch(r'attachment; filename="mahsulotlar-\d{8}-\d{4}\.csv"', uzbek["Content-Disposition"])
    english = staff_client.get(PRODUCTS, {"export": "csv"}, HTTP_ACCEPT_LANGUAGE="en")
    assert re.fullmatch(r'attachment; filename="products-\d{8}-\d{4}\.csv"', english["Content-Disposition"])


@pytest.mark.django_db
def test_category_headers_read_as_the_category_list(staff_client: APIClient) -> None:
    CategoryFactory.create(slug="serums", name_uz="Serumlar", position=0)
    rows = {
        language: list(
            csv.reader(
                StringIO(
                    staff_client.get(
                        CATEGORIES, {"export": "csv"}, HTTP_ACCEPT_LANGUAGE=language
                    ).content.decode("utf-8-sig")
                ),
                delimiter=";",
            )
        )
        for language in ("uz", "ru", "en")
    }
    assert rows["uz"][0] == ["Slug", "Nomi", "Tartib", "Faol", "Mahsulotlar", "Rasm"]
    assert rows["ru"][0] == ["Slug", "Название", "Порядок", "Активна", "Товары", "Изображение"]
    assert rows["en"][0] == ["Slug", "Name", "Position", "Active", "Products", "Image"]
    # outside an export the plain message is unchanged (the ops console's history label, a person's name)
    assert uzsource.tr("Nomi", "ru") == "Имя"
    assert translation.get_language() == "uz"


@pytest.mark.parametrize(
    ("resource", "text", "russian", "english"),
    [
        ("products", "Nomi", "Название", "Name"),
        ("segments", "Nomi", "Название", "Name"),
        ("categories", "Faol", "Активна", "Active"),
        ("banners", "Faol", "Активен", "Active"),
        ("products", "Faol", "Активен", "Active"),
        ("specialists", "Faol", "Активен", "Active"),
        ("specialists", "Nofaol", "Неактивен", "Inactive"),
        ("customers", "Faol", "Активен", "Active"),
        ("segment_members", "Faol", "Активен", "Active"),
    ],
)
def test_list_specific_renderings(resource: str, text: str, russian: str, english: str) -> None:
    with uzsource.rendering_context(export_context(resource)):
        assert (uzsource.tr(text, "ru"), uzsource.tr(text, "en"), uzsource.tr(text, "uz")) == (
            russian,
            english,
            text,
        )
    assert uzsource.tr(text, "ru") != text  # the plain message is translated too


def test_inactive_has_a_plain_translation() -> None:
    with translation.override("ru"):
        assert labels.active_label(False) == "Неактивный"
    with translation.override("en"):
        assert labels.active_label(False) == "Inactive"
    assert labels.active_label(False) == "Nofaol"


def test_every_export_context_names_an_export() -> None:
    resources = {view.export_resource for view in export_views()}
    contexts = {
        message.msgctxt
        for message in console_catalog.collect().messages()
        if message.msgctxt is not None and message.msgctxt.startswith("export ")
    }
    assert contexts and contexts <= {export_context(resource) for resource in resources}
