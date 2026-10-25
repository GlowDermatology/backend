"""Contract §2.4 ``categories/``: list, create with the slug rules, retrieve, PATCH, delete and reorder.

Shapes, codes and messages follow the contract table literally.
"""

import csv
from io import StringIO
from typing import Any

import pytest
from rest_framework.test import APIClient

from glowderma.accounts.models import StaffUser
from glowderma.catalog.models import Banner, Category
from glowderma.catalog.tests.factories import BannerFactory, CategoryFactory, ProductFactory
from glowderma.catalog.tests.helpers import errors_of
from glowderma.core import timeutils
from glowderma.core.models import AuditEntry

pytestmark = pytest.mark.django_db

CATEGORIES = "/api/v1/admin/categories/"


def slugs(response: Any) -> list[str]:
    return [item["slug"] for item in response.json()["results"]]


def test_list_ordered_by_position_with_product_count_and_filter(staff_client: APIClient) -> None:
    toners = CategoryFactory.create(slug="toners", position=1, is_active=False)
    serums = CategoryFactory.create(slug="serums", name_uz="Serumlar", name_ru="Сыворотки", position=0)
    ProductFactory.create(category=serums, status="active")
    ProductFactory.create(category=serums, status="hidden")
    response = staff_client.get(CATEGORIES)
    assert response.status_code == 200
    body = response.json()
    assert (body["count"], body["page"], body["page_size"], body["page_count"]) == (2, 1, 20, 1)
    assert body["results"][0] == {
        "slug": "serums",
        "name": {"uz": "Serumlar", "ru": "Сыворотки", "en": ""},
        "position": 0,
        "is_active": True,
        "product_count": 2,
        "created_at": timeutils.isoformat(serums.created_at),
        "updated_at": timeutils.isoformat(serums.updated_at),
    }
    assert body["results"][1]["slug"] == "toners" and body["results"][1]["product_count"] == 0
    assert slugs(staff_client.get(CATEGORIES, {"is_active": "false"})) == [toners.slug]
    assert slugs(staff_client.get(CATEGORIES, {"is_active": "1"})) == ["serums"]
    assert staff_client.get(CATEGORIES, {"page_size": "50"}).status_code == 200
    assert errors_of(staff_client.get(CATEGORIES, {"page_size": "8"}))["page_size"][0] == "invalid_choice"
    assert errors_of(staff_client.get(CATEGORIES, {"is_active": "x"}))["is_active"][0] == "invalid_choice"


def test_export(staff_client: APIClient) -> None:
    serums = CategoryFactory.create(slug="serums", name_uz="=Serumlar", position=0)
    ProductFactory.create(category=serums)
    CategoryFactory.create(slug="toners", name_uz="Tonerlar", position=1, is_active=False)
    response = staff_client.get(CATEGORIES, {"export": "csv"})
    assert response.status_code == 200
    assert response["Content-Disposition"].startswith('attachment; filename="kategoriyalar-')
    rows = list(csv.reader(StringIO(response.content.decode("utf-8-sig")), delimiter=";"))
    assert rows == [
        ["Slug", "Nomi", "Tartib", "Faol", "Mahsulotlar"],
        ["serums", "'=Serumlar", "0", "Ha", "1"],
        ["toners", "Tonerlar", "1", "Yo‘q", "0"],
    ]
    assert (AuditEntry.objects.get(action="export.downloaded").after or {})["resource"] == "categories"
    assert staff_client.get(CATEGORIES, {"export": "xlsx"}).status_code == 200


def test_create_appends_last_and_audits(staff_client: APIClient, staff: StaffUser) -> None:
    CategoryFactory.create(slug="serums", position=0)
    response = staff_client.post(
        CATEGORIES, {"slug": "toners", "name": {"uz": "Tonerlar", "ru": "", "en": ""}}
    )
    assert response.status_code == 201, response.json()
    category = Category.objects.get(slug="toners")
    assert response.json() == {
        "slug": "toners",
        "name": {"uz": "Tonerlar", "ru": "", "en": ""},
        "position": 1,
        "is_active": True,
        "product_count": 0,
        "created_at": timeutils.isoformat(category.created_at),
        "updated_at": timeutils.isoformat(category.updated_at),
    }
    entry = AuditEntry.objects.get(action="category.created")
    assert entry.entity_type == "category" and entry.entity_id == "toners" and entry.actor_id == staff.pk
    assert (entry.after or {})["name.uz"] == "Tonerlar" and (entry.after or {})["position"] == 1
    # ru/en keys are mandatory (core i18n_text: "" means not filled, never absent)
    response = staff_client.post(CATEGORIES, {"slug": "sun", "name": {"uz": "Quyosh"}, "is_active": False})
    assert response.status_code == 400 and set(errors_of(response)) == {"name.ru", "name.en"}
    body = {"slug": "sun", "name": {"uz": "Quyosh", "ru": "", "en": ""}, "is_active": False}
    response = staff_client.post(CATEGORIES, body)
    assert response.status_code == 201 and response.json()["position"] == 2
    assert response.json()["is_active"] is False


@pytest.mark.parametrize(
    ("body", "field", "code", "message"),
    [
        (
            {"slug": "Serums", "name": {"uz": "Serumlar"}},
            "slug",
            "invalid",
            "Slug faqat kichik lotin harflari, raqamlar va chiziqchadan iborat bo‘lsin (2–40 belgi).",
        ),
        (
            {"slug": "s", "name": {"uz": "Serumlar"}},
            "slug",
            "invalid",
            "Slug faqat kichik lotin harflari, raqamlar va chiziqchadan iborat bo‘lsin (2–40 belgi).",
        ),
        (
            {"slug": "a" * 41, "name": {"uz": "Serumlar"}},
            "slug",
            "invalid",
            "Slug faqat kichik lotin harflari, raqamlar va chiziqchadan iborat bo‘lsin (2–40 belgi).",
        ),
        ({"slug": "order", "name": {"uz": "Serumlar"}}, "slug", "invalid", "Bu slug band."),
        ({"slug": "serums", "name": {"uz": "Serumlar"}}, "slug", "unique", "Bu qiymat band."),
        ({"name": {"uz": "Serumlar"}}, "slug", "required", "Majburiy maydon."),
        ({"slug": "toners"}, "name", "required", "Majburiy maydon."),
        ({"slug": "toners", "name": {"ru": "x"}}, "name.uz", "required", "Majburiy maydon."),
        ({"slug": "toners", "name": {"uz": ""}}, "name.uz", "blank", "Maydonni to‘ldiring."),
        ({"slug": "toners", "name": {"uz": "T"}}, "name.uz", "min_length", "Kamida 2 ta belgi kiriting."),
        ({"slug": "toners", "name": {"uz": "x" * 61}}, "name.uz", "max_length", "60 belgidan oshmasin."),
        (
            {"slug": "toners", "name": {"uz": "Tonerlar", "ru": "x" * 61, "en": ""}},
            "name.ru",
            "max_length",
            "60 belgidan oshmasin.",
        ),
        (
            {"slug": "toners", "name": {"uz": "Tonerlar", "ru": "", "en": "x" * 61}},
            "name.en",
            "max_length",
            "60 belgidan oshmasin.",
        ),
        (
            {"slug": "toners", "name": {"uz": "Tonerlar", "ru": "", "en": ""}, "is_active": "maybe"},
            "is_active",
            "invalid",
            "Noto‘g‘ri qiymat.",
        ),
    ],
)
def test_create_validation_table(
    staff_client: APIClient, body: dict[str, Any], field: str, code: str, message: str
) -> None:
    CategoryFactory.create(slug="serums")
    response = staff_client.post(CATEGORIES, body)
    assert response.status_code == 400, response.json()
    assert errors_of(response)[field] == (code, message)
    assert Category.objects.count() == 1


def test_retrieve_patch_and_slug_is_immutable(staff_client: APIClient) -> None:
    category = CategoryFactory.create(slug="serums", name_uz="Serumlar", name_ru="Сыворотки")
    url = f"{CATEGORIES}serums/"
    assert staff_client.get(url).status_code == 200 and staff_client.get(url).json()["slug"] == "serums"
    assert staff_client.get(f"{CATEGORIES}missing/").status_code == 404
    assert staff_client.get(f"{CATEGORIES}order/").status_code == 405  # the reorder route, not a slug
    response = staff_client.patch(url, {"name": {"en": "Serums"}, "is_active": False})
    assert response.status_code == 200, response.json()
    assert response.json()["name"] == {"uz": "Serumlar", "ru": "Сыворотки", "en": "Serums"}
    assert response.json()["is_active"] is False
    entry = AuditEntry.objects.get(action="category.updated")
    assert entry.before == {"name.en": "", "is_active": True}
    assert entry.after == {"name.en": "Serums", "is_active": False}
    response = staff_client.patch(url, {"slug": "new-slug"})
    assert response.status_code == 400
    assert errors_of(response)["slug"] == ("read_only", "Bu maydonni o‘zgartirib bo‘lmaydi.")
    assert Category.objects.get(pk=category.pk).slug == "serums"
    response = staff_client.patch(url, {"name": {"uz": "S"}})
    assert errors_of(response)["name.uz"] == ("min_length", "Kamida 2 ta belgi kiriting.")
    assert staff_client.patch(f"{CATEGORIES}missing/", {"is_active": True}).status_code == 404


def test_delete(staff_client: APIClient, django_capture_on_commit_callbacks: Any) -> None:
    serums = CategoryFactory.create(slug="serums", position=0)
    toners = CategoryFactory.create(slug="toners", position=1)
    sun = CategoryFactory.create(slug="sun", position=2)
    ProductFactory.create(category=serums)
    banner = BannerFactory.create(link_kind="category", link_category=toners)
    response = staff_client.delete(f"{CATEGORIES}serums/")
    assert response.status_code == 409
    assert response.json()["code"] == "conflict"
    assert response.json()["message"] == "Kategoriyada mahsulotlar bor."
    assert Category.objects.filter(slug="serums").exists()
    with django_capture_on_commit_callbacks(execute=True):
        response = staff_client.delete(f"{CATEGORIES}toners/")
    assert response.status_code == 204
    assert not Category.objects.filter(slug="toners").exists()
    assert list(Category.objects.order_by("position").values_list("slug", "position")) == [
        ("serums", 0),
        ("sun", 1),
    ]
    banner = Banner.objects.get(pk=banner.pk)
    assert banner.link_kind == "none" and banner.link_category_id is None
    entry = AuditEntry.objects.get(action="category.deleted")
    assert entry.entity_id == "toners" and (entry.before or {})["position"] == 1
    assert staff_client.delete(f"{CATEGORIES}toners/").status_code == 404
    assert sun.pk  # still there


def test_reorder(staff_client: APIClient) -> None:
    for position, slug in enumerate(("serums", "toners", "sun")):
        CategoryFactory.create(slug=slug, position=position)
    url = f"{CATEGORIES}order/"
    response = staff_client.put(url, {"slugs": ["sun", "serums", "toners"]})
    assert response.status_code == 200, response.json()
    assert [(item["slug"], item["position"]) for item in response.json()["results"]] == [
        ("sun", 0),
        ("serums", 1),
        ("toners", 2),
    ]
    assert set(response.json()["results"][0]) == {
        "slug",
        "name",
        "position",
        "is_active",
        "product_count",
        "created_at",
        "updated_at",
    }
    entry = AuditEntry.objects.get(action="category.reordered")
    assert entry.before == {"slugs": ["serums", "toners", "sun"]}
    assert entry.after == {"slugs": ["sun", "serums", "toners"]}
    for body in (["sun", "serums"], ["sun", "serums", "toners", "x"], ["sun", "sun", "serums"]):
        response = staff_client.put(url, {"slugs": body})
        assert response.status_code == 400, body
        assert errors_of(response)["slugs"] == ("mismatch", "Ro‘yxat joriy elementlarga mos emas.")
    assert errors_of(staff_client.put(url, {}))["slugs"] == ("required", "Majburiy maydon.")
    assert slugs(staff_client.get(CATEGORIES)) == ["sun", "serums", "toners"]
