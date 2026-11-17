"""The catalog admin API in Russian and English (feat/admin-ux, DESIGN §G): the marked messages of
``catalog.services`` and the admin serializers, and the banner export's link labels, follow the request's
``Accept-Language``; Uzbek stays byte for byte the source text."""

from __future__ import annotations

import csv
from collections.abc import Iterator
from io import StringIO
from typing import Any

import pytest
from rest_framework.test import APIClient

from glowderma.catalog.tests.factories import (
    BannerFactory,
    CategoryFactory,
    ProductFactory,
    ProductImageFactory,
)
from glowderma.core import uzsource

pytestmark = pytest.mark.django_db
PRODUCTS = "/api/v1/admin/products/"
CATEGORIES = "/api/v1/admin/categories/"
BANNERS = "/api/v1/admin/banners/"
# The texts these tests read, with their translations (the committed catalogs carry them once
# ``console_messages`` has run; the tests do not wait for that).
TRANSLATIONS: dict[str, dict[str, str]] = {
    "Kategoriyada mahsulotlar bor.": {
        "en": "The category still has products.",
        "ru": "В категории есть товары.",
    },
    "Narx kamida 1 000 so‘m bo‘lsin.": {
        "en": "The price must be at least 1,000 UZS.",
        "ru": "Цена должна быть не меньше 1 000 сум.",
    },
    "Faol mahsulot uchun kategoriyani tanlang.": {
        "en": "Select a category for an active product.",
        "ru": "Выберите категорию для активного товара.",
    },
    "mahsulot #%(id)s": {"en": "product #%(id)s", "ru": "товар #%(id)s"},
    "kategoriya %(slug)s": {"en": "category %(slug)s", "ru": "категория %(slug)s"},
}


@pytest.fixture
def translations(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    real = uzsource.catalog

    def catalog(language: str) -> dict[str, str]:
        if language not in uzsource.TARGET_LANGUAGES:
            return {}
        return {**real(language), **{uz: texts[language] for uz, texts in TRANSLATIONS.items()}}

    monkeypatch.setattr(uzsource, "catalog", catalog)
    uzsource.templates.cache_clear()
    yield
    uzsource.templates.cache_clear()


def test_a_service_conflict_is_rendered_in_the_request_language(
    translations: None, staff_client: APIClient
) -> None:
    ProductFactory.create(category=CategoryFactory.create(slug="serums"))
    russian = staff_client.delete(f"{CATEGORIES}serums/", HTTP_ACCEPT_LANGUAGE="ru")
    assert russian.status_code == 409 and russian["Content-Language"] == "ru"
    assert (russian.json()["code"], russian.json()["message"]) == ("conflict", "В категории есть товары.")
    uzbek = staff_client.delete(f"{CATEGORIES}serums/")
    assert uzbek.json()["message"] == "Kategoriyada mahsulotlar bor."


def test_serializer_and_service_field_errors_are_rendered_in_the_request_language(
    translations: None, staff_client: APIClient
) -> None:
    product = ProductFactory.create(status="hidden")
    response = staff_client.patch(
        f"{PRODUCTS}{product.pk}/", {"price": 500}, format="json", HTTP_ACCEPT_LANGUAGE="en"
    )
    assert response.status_code == 400
    assert {"field": "price", "code": "min_value", "message": "The price must be at least 1,000 UZS."} in (
        response.json()["errors"]
    )
    active = ProductFactory.create(status="active", category=CategoryFactory.create(slug="toners"))
    ProductImageFactory.create(product=active)
    cleared = staff_client.patch(
        f"{PRODUCTS}{active.pk}/", {"category": None}, format="json", HTTP_ACCEPT_LANGUAGE="ru"
    )
    assert cleared.json()["errors"] == [
        {
            "field": "category",
            "code": "required_for_active",
            "message": "Выберите категорию для активного товара.",
        }
    ]
    uzbek = staff_client.patch(f"{PRODUCTS}{product.pk}/", {"price": 500}, format="json")
    assert {"field": "price", "code": "min_value", "message": "Narx kamida 1 000 so‘m bo‘lsin."} in (
        uzbek.json()["errors"]
    )


def test_the_banner_export_labels_its_links_in_the_request_language(
    translations: None, staff_client: APIClient
) -> None:
    product = ProductFactory.create(status="active")
    BannerFactory.create(position=0, link_kind="product", link_product=product)
    BannerFactory.create(
        position=1, link_kind="category", link_category=CategoryFactory.create(slug="serums")
    )

    def links(**headers: Any) -> list[str]:
        response = staff_client.get(BANNERS, {"export": "csv"}, **headers)
        assert response.status_code == 200
        rows = list(csv.reader(StringIO(response.content.decode("utf-8-sig")), delimiter=";"))
        return [row[2] for row in rows[1:]]

    assert links(HTTP_ACCEPT_LANGUAGE="en") == [f"product #{product.pk}", "category serums"]
    assert links(HTTP_ACCEPT_LANGUAGE="ru") == [f"товар #{product.pk}", "категория serums"]
    assert links() == [f"mahsulot #{product.pk}", "kategoriya serums"]
