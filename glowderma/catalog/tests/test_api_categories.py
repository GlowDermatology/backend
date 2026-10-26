"""Contract §2.4 ``categories/``: list, create with the slug rules, retrieve, PATCH, delete and reorder.

Shapes, codes and messages follow the contract table literally.
"""

import csv
import threading
from io import StringIO
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from django.conf import settings
from django.db import connection
from django.test.utils import CaptureQueriesContext
from rest_framework.exceptions import ValidationError
from rest_framework.test import APIClient

from glowderma.accounts.models import StaffUser
from glowderma.catalog.models import Banner, Category
from glowderma.catalog.tests.factories import BannerFactory, CategoryFactory, ProductFactory
from glowderma.catalog.tests.helpers import errors_of, upload
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
        "image_url": None,
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
        ["Slug", "Nomi", "Tartib", "Faol", "Mahsulotlar", "Rasm"],
        ["serums", "'=Serumlar", "0", "Ha", "1", ""],
        ["toners", "Tonerlar", "1", "Yo‘q", "0", ""],
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
        "image_url": None,
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
        "image_url",
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


# --- gap ADM-5.1: the category image (decision GC-4) ------------------------------------------------------


def media_url(name: str) -> str:
    return f"{settings.PUBLIC_MEDIA_BASE_URL.rstrip('/')}/{name}"


def test_create_multipart_with_an_image(staff_client: APIClient, staff: StaffUser) -> None:
    response = staff_client.post(
        CATEGORIES,
        {
            "slug": "masks",
            "name.uz": "Niqoblar",
            "name.ru": "Маски",
            "is_active": "false",
            "image": upload("JPEG", size=(3000, 1500)),
        },
        format="multipart",
    )
    assert response.status_code == 201, response.json()
    category = Category.objects.get(slug="masks")
    name = str(category.image.name)
    assert name.startswith(f"categories/{category.pk}/") and name.endswith(".jpg")
    assert response.json()["image_url"] == media_url(name)
    assert response.json()["name"] == {"uz": "Niqoblar", "ru": "Маски", "en": ""}
    assert response.json()["is_active"] is False
    from PIL import Image

    with Image.open(category.image.path) as stored:  # re-encoded, longest side ≤ 2400 px
        assert stored.size == (2400, 1200)
    after = AuditEntry.objects.get(action="category.created").after or {}
    assert after["image"] == name
    # the export carries the image URL (review nit: column Rasm)
    export = staff_client.get(CATEGORIES, {"export": "csv"}).content.decode("utf-8-sig")
    rows = list(csv.reader(StringIO(export), delimiter=";"))
    assert rows[0][-1] == "Rasm" and rows[1][-1] == media_url(name)
    # the customer catalogue shows it (customer contract §2.3 catalog/categories/ image_url)
    ProductFactory.create(category=category, status="active", price=50_000)
    category.is_active = True
    category.save()
    rows = APIClient().get("/api/v1/catalog/categories/").json()["results"]
    assert rows[0]["image_url"] == media_url(name)


@pytest.mark.parametrize(
    ("image", "code", "message"),
    [
        (
            upload(content=b"not an image", name="x.png"),
            "unsupported_image",
            "Faqat JPEG, PNG yoki WebP rasm.",
        ),
        (
            upload(content=b"\x89PNG" + b"\0" * (5 * 1024 * 1024 - 3), name="big.png"),
            "file_too_large",
            "Rasm 5 MB dan oshmasin.",
        ),
    ],
)
def test_create_refuses_a_bad_image_and_writes_nothing(
    staff_client: APIClient, image: Any, code: str, message: str
) -> None:
    response = staff_client.post(
        CATEGORIES, {"slug": "masks", "name.uz": "Niqoblar", "image": image}, format="multipart"
    )
    assert response.status_code == 400
    assert errors_of(response)["image"] == (code, message)
    assert not Category.objects.exists() and not AuditEntry.objects.filter(action="category.created").exists()


def test_a_failed_write_removes_the_image_it_stored(staff: StaffUser) -> None:
    """Review nit: the audit write failing rolls the row back and removes the file written for it."""
    from glowderma.catalog import services

    category = CategoryFactory.create(slug="serums", name_uz="Serumlar")
    media = Path(settings.MEDIA_ROOT) / "categories" / str(category.pk)
    before = set(media.glob("*")) if media.exists() else set()
    with (
        patch("glowderma.catalog.services.audit.record", side_effect=RuntimeError("audit down")),
        pytest.raises(RuntimeError),
    ):
        services.update_category(category, image=upload("PNG"), actor=staff)
    assert Category.objects.get(pk=category.pk).image.name == ""
    assert (set(media.glob("*")) if media.exists() else set()) == before
    with (
        patch("glowderma.catalog.services.audit.record", side_effect=RuntimeError("audit down")),
        pytest.raises(RuntimeError),
    ):
        services.create_category("masks", {"uz": "Niqoblar"}, image=upload("PNG"), actor=staff)
    assert not Category.objects.filter(slug="masks").exists()
    leftovers = [p for p in (Path(settings.MEDIA_ROOT) / "categories").rglob("*") if p.is_file()]
    assert leftovers == []


def test_patch_replaces_and_removes_the_image(
    staff_client: APIClient, django_capture_on_commit_callbacks: Any
) -> None:
    CategoryFactory.create(slug="serums", name_uz="Serumlar")
    url = f"{CATEGORIES}serums/"
    with django_capture_on_commit_callbacks(execute=True):
        first = staff_client.patch(url, {"image": upload("PNG", size=(40, 20))}, format="multipart")
    assert first.status_code == 200, first.json()
    category = Category.objects.get(slug="serums")
    old_path = Path(category.image.path)
    assert old_path.exists() and first.json()["image_url"] == media_url(str(category.image.name))
    # the same bytes again: nothing moves (content-addressed name), the file stays
    with django_capture_on_commit_callbacks(execute=True):
        again = staff_client.patch(url, {"image": upload("PNG", size=(40, 20))}, format="multipart")
    assert again.json()["image_url"] == first.json()["image_url"] and old_path.exists()
    # a new image replaces it; the old file is removed after commit; the names arrive dotted in multipart
    with django_capture_on_commit_callbacks(execute=True):
        second = staff_client.patch(
            url, {"image": upload("WEBP", size=(30, 30)), "name.en": "Serums"}, format="multipart"
        )
    assert second.status_code == 200, second.json()
    category.refresh_from_db()
    new_path = Path(category.image.path)
    assert str(category.image.name).endswith(".webp") and new_path.exists() and not old_path.exists()
    assert second.json()["name"]["en"] == "Serums"
    entry = AuditEntry.objects.filter(action="category.updated").latest("id")
    assert entry.before == {"name.en": "", "image": str(old_path.relative_to(settings.MEDIA_ROOT))}
    assert entry.after == {"name.en": "Serums", "image": category.image.name}
    # both at once is refused
    response = staff_client.patch(url, {"image": upload("PNG"), "remove_image": "true"}, format="multipart")
    assert errors_of(response)["remove_image"] == ("invalid", "Noto‘g‘ri qiymat.")
    # remove (JSON): image_url null, the file removed after commit
    with django_capture_on_commit_callbacks(execute=True):
        removed = staff_client.patch(url, {"remove_image": True})
    assert removed.status_code == 200 and removed.json()["image_url"] is None
    assert Category.objects.get(slug="serums").image.name == "" and not new_path.exists()
    entry = AuditEntry.objects.filter(action="category.updated").latest("id")
    assert entry.after == {"image": None}
    # removing again: nothing to remove, still 200
    assert staff_client.patch(url, {"remove_image": True}).json()["image_url"] is None
    response = staff_client.patch(url, {"remove_image": "maybe"})
    assert errors_of(response)["remove_image"] == ("invalid", "Noto‘g‘ri qiymat.")
    # review nit: only a JSON boolean (or true/false/1/0 in multipart), not DRF's truthy set
    for loose in ("yes", "on", 1):
        response = staff_client.patch(url, {"remove_image": loose}, format="json")
        assert errors_of(response)["remove_image"] == ("invalid", "Noto‘g‘ri qiymat."), loose
    assert staff_client.patch(url, {"remove_image": "false"}, format="multipart").status_code == 200


def test_category_bodies_are_json_or_multipart_only(staff_client: APIClient) -> None:
    CategoryFactory.create(slug="serums")
    body = "slug=masks&name.uz=Niqoblar"
    for response in (
        staff_client.post(CATEGORIES, body, content_type="application/x-www-form-urlencoded"),
        staff_client.patch(f"{CATEGORIES}serums/", body, content_type="application/x-www-form-urlencoded"),
        staff_client.put(f"{CATEGORIES}order/", {"slugs": ["serums"]}, format="multipart"),
    ):
        assert response.status_code == 415, response.json()


def test_list_renders_images_without_extra_queries(staff_client: APIClient) -> None:
    for index in range(3):
        CategoryFactory.create(slug=f"cat-{index}", position=index)

    def count() -> int:
        with CaptureQueriesContext(connection) as queries:
            assert staff_client.get(CATEGORIES).status_code == 200
        return len(queries)

    count()  # warm-up: the first authenticated request of the session touches it
    baseline = count()
    for index, category in enumerate(Category.objects.order_by("position")):
        staff_client.patch(
            f"{CATEGORIES}{category.slug}/",
            {"image": upload("PNG", size=(10 + index, 10))},
            format="multipart",
        )
        CategoryFactory.create(slug=f"more-{index}", position=10 + index)
    assert count() == baseline
    assert all(item["image_url"] for item in staff_client.get(CATEGORIES).json()["results"][:3])
