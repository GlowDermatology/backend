"""Contract §2.4 ``categories/``: list, create with the slug rules, retrieve, PATCH, delete and reorder.

Shapes, codes and messages follow the contract table literally.
"""

import csv
import threading
from io import StringIO
from typing import Any
from unittest.mock import patch

import pytest
from django.db import connection
from rest_framework.exceptions import ValidationError
from rest_framework.test import APIClient

from glowderma.accounts.models import StaffUser
from glowderma.catalog.models import Banner, Category
from glowderma.catalog.tests.factories import BannerFactory, CategoryFactory, ProductFactory
from glowderma.catalog.tests.helpers import errors_of
from glowderma.core import timeutils
from glowderma.core.api.exceptions import field_error
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
    # contract §2.4: only ``name.uz`` is required; the other languages default to "" on create
    response = staff_client.post(CATEGORIES, {"slug": "sun", "name": {"uz": "Quyosh"}, "is_active": False})
    assert response.status_code == 201, response.json()
    assert response.json()["name"] == {"uz": "Quyosh", "ru": "", "en": ""}
    assert response.json()["position"] == 2 and response.json()["is_active"] is False


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


def test_create_with_uz_only_name_defaults_other_languages_to_empty(staff_client: APIClient) -> None:
    """Contract §2.4: only ``name.uz`` is required; ``ru`` / ``en`` missing on create → ``""``."""
    response = staff_client.post(CATEGORIES, {"slug": "ab", "name": {"uz": "Ab"}})
    assert response.status_code == 201, response.json()
    assert response.json()["name"] == {"uz": "Ab", "ru": "", "en": ""}
    category = Category.objects.get(slug="ab")
    assert (category.name_uz, category.name_ru, category.name_en) == ("Ab", "", "")
    # PATCH keeps the partial semantics: a lone ``uz`` leaves the other languages alone
    category.name_ru = "Аб"
    category.save(update_fields=["name_ru"])
    response = staff_client.patch(f"{CATEGORIES}ab/", {"name": {"uz": "Abc"}})
    assert response.status_code == 200 and response.json()["name"] == {"uz": "Abc", "ru": "Аб", "en": ""}


def test_reorder_empty_list(staff_client: APIClient) -> None:
    """``[]`` is the exact (empty) set when there are no categories; otherwise it is a ``mismatch``."""
    url = f"{CATEGORIES}order/"
    response = staff_client.put(url, {"slugs": []})
    assert response.status_code == 200, response.json()
    assert response.json() == {"results": []}
    CategoryFactory.create(slug="serums")
    response = staff_client.put(url, {"slugs": []})
    assert response.status_code == 400
    assert errors_of(response)["slugs"] == ("mismatch", "Ro‘yxat joriy elementlarga mos emas.")


def test_create_losing_the_slug_race_is_a_unique_error(staff_client: APIClient, staff: StaffUser) -> None:
    """The serializer pre-check passed, the row appeared before the INSERT: 400 ``unique``, never a 500."""
    from glowderma.catalog import services

    original = services.create_category

    def create_after_competitor(slug: str, name: dict[str, str], **kwargs: Any) -> Category:
        CategoryFactory.create(slug=slug)  # the competitor commits between validation and the service
        return original(slug, name, **kwargs)

    with patch("glowderma.catalog.api.admin.views.services.create_category", create_after_competitor):
        response = staff_client.post(CATEGORIES, {"slug": "serums", "name": {"uz": "Serumlar"}})
    assert response.status_code == 400, response.json()
    assert errors_of(response)["slug"] == ("unique", "Bu qiymat band.")
    assert Category.objects.filter(slug="serums").count() == 1
    assert not AuditEntry.objects.filter(action="category.created").exists()
    # the service itself reports the same error when called directly
    with pytest.raises(ValidationError) as excinfo:
        services.create_category("serums", {"uz": "Serumlar"}, actor=staff)
    assert excinfo.value.detail == {"slug": [field_error("unique")]}


@pytest.mark.django_db(transaction=True)
def test_concurrent_creates_with_the_same_slug(staff: StaffUser) -> None:
    from glowderma.catalog import services

    start = threading.Event()
    outcomes: list[str] = []
    lock = threading.Lock()

    def worker() -> None:
        try:
            assert start.wait(10)
            services.create_category("serums", {"uz": "Serumlar"}, actor=staff)
            result = "ok"
        except ValidationError as exc:
            detail: Any = exc.detail
            result = str(detail["slug"][0].code)
        except BaseException as exc:  # noqa: BLE001 - surfaced to the main thread below
            result = type(exc).__name__
        finally:
            connection.close()
        with lock:
            outcomes.append(result)

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for thread in threads:
        thread.start()
    start.set()
    for thread in threads:
        thread.join(10)
        assert not thread.is_alive()
    assert sorted(outcomes) == ["ok", "unique"]
    assert Category.objects.filter(slug="serums").count() == 1
