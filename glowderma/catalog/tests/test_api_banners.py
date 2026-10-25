"""Contract §2.5 ``banners/``: multipart create with dotted keys, link and date rules, JSON/multipart PATCH,
delete, reorder, export."""

import csv
from datetime import datetime
from io import StringIO
from pathlib import Path
from typing import Any

import pytest
from django.conf import settings
from django.http import QueryDict
from rest_framework.test import APIClient

from glowderma.accounts.models import StaffUser
from glowderma.catalog.api.admin.views import form_to_nested
from glowderma.catalog.models import Banner
from glowderma.catalog.tests.factories import BannerFactory, CategoryFactory, ProductFactory
from glowderma.catalog.tests.helpers import errors_of, upload
from glowderma.core import timeutils
from glowderma.core.models import AuditEntry
from glowderma.core.timeutils import TASHKENT

pytestmark = pytest.mark.django_db

BANNERS = "/api/v1/admin/banners/"


def media_url(name: str | None) -> str:
    return f"{settings.PUBLIC_MEDIA_BASE_URL.rstrip('/')}/{name}"


def form(**overrides: Any) -> dict[str, Any]:
    data: dict[str, Any] = {"image": upload("WEBP", size=(32, 12)), "title.uz": "Kuzgi chegirmalar"}
    data.update(overrides)
    return data


def test_form_to_nested_undots_keys_and_nulls_empty_targets() -> None:
    data = QueryDict(mutable=True)
    data.update(
        {
            "title.uz": "Kuz",
            "title.ru": "",
            "link.kind": "product",
            "link.product_id": "",
            "link.category": "",
            "starts_at": "",
            "is_active": "false",
        }
    )
    assert form_to_nested(data) == {
        "title": {"uz": "Kuz", "ru": ""},
        "link": {"kind": "product", "product_id": None, "category": None},
        "starts_at": None,
        "is_active": "false",
    }
    assert form_to_nested({"title": {"uz": "Kuz"}}) == {"title": {"uz": "Kuz"}}
    assert form_to_nested("nonsense") == {}


def test_create_multipart_full_shape(staff_client: APIClient, staff: StaffUser) -> None:
    product = ProductFactory.create(status="active")
    BannerFactory.create(position=0)
    body = form(
        **{
            "title.ru": "Осенние скидки",
            "title.en": "",
            "link.kind": "product",
            "link.product_id": str(product.pk),
            "is_active": "1",
            "starts_at": "2026-11-01T00:00:00+05:00",
            "ends_at": "2026-11-30T23:59:00Z",
        }
    )
    response = staff_client.post(BANNERS, body, format="multipart")
    assert response.status_code == 201, response.json()
    banner = Banner.objects.get(title_uz="Kuzgi chegirmalar")
    assert str(banner.image.name).startswith(f"banners/{banner.pk}/") and str(banner.image.name).endswith(
        ".webp"
    )
    assert Path(banner.image.path).is_relative_to(settings.MEDIA_ROOT) and Path(banner.image.path).exists()
    assert (banner.image_width, banner.image_height) == (32, 12)
    assert response.json() == {
        "id": banner.pk,
        "title": {"uz": "Kuzgi chegirmalar", "ru": "Осенние скидки", "en": ""},
        "image_url": media_url(banner.image.name),
        "link": {"kind": "product", "product_id": product.pk, "category": None},
        "is_active": True,
        "starts_at": "2026-11-01T00:00:00+05:00",
        "ends_at": "2026-12-01T04:59:00+05:00",
        "position": 1,
        "created_at": timeutils.isoformat(banner.created_at),
        "updated_at": timeutils.isoformat(banner.updated_at),
    }
    entry = AuditEntry.objects.get(action="banner.created")
    assert entry.entity_id == str(banner.pk) and entry.actor_id == staff.pk
    assert (entry.after or {})["link.product_id"] == product.pk and (entry.after or {})["position"] == 1


def test_create_minimal_defaults(staff_client: APIClient) -> None:
    response = staff_client.post(BANNERS, form(**{"title.ru": "", "title.en": ""}), format="multipart")
    assert response.status_code == 201, response.json()
    data = response.json()
    assert data["link"] == {"kind": "none", "product_id": None, "category": None}
    assert data["is_active"] is True and data["starts_at"] is None and data["ends_at"] is None
    assert data["position"] == 0


def test_create_with_category_link_and_empty_strings_as_null(staff_client: APIClient) -> None:
    CategoryFactory.create(slug="serums")
    body = form(
        **{
            "title.ru": "",
            "title.en": "",
            "link.kind": "category",
            "link.category": "serums",
            "link.product_id": "",
            "starts_at": "",
            "ends_at": "",
        }
    )
    response = staff_client.post(BANNERS, body, format="multipart")
    assert response.status_code == 201, response.json()
    assert response.json()["link"] == {"kind": "category", "product_id": None, "category": "serums"}
    assert response.json()["starts_at"] is None


@pytest.mark.parametrize(
    ("overrides", "field", "code", "message"),
    [
        ({"image": None}, "image", "required", "Majburiy maydon."),
        ({"image": "spoof"}, "image", "unsupported_image", "Faqat JPEG, PNG yoki WebP rasm."),
        ({"image": "huge"}, "image", "file_too_large", "Rasm 5 MB dan oshmasin."),
        ({"title.uz": ""}, "title.uz", "blank", "Maydonni to‘ldiring."),
        ({"title.uz": "x" * 81}, "title.uz", "max_length", "80 belgidan oshmasin."),
        ({"title.ru": "x" * 81}, "title.ru", "max_length", "80 belgidan oshmasin."),
        ({"title.en": "x" * 81}, "title.en", "max_length", "80 belgidan oshmasin."),
        ({"link.kind": "url"}, "link.kind", "invalid_choice", "Ruxsat etilmagan qiymat."),
        ({"link.kind": "product"}, "link.product_id", "required", "Majburiy maydon."),
        (
            {"link.kind": "product", "link.product_id": "999999"},
            "link.product_id",
            "does_not_exist",
            "Topilmadi.",
        ),
        (
            {"link.kind": "product", "link.product_id": "HIDDEN"},
            "link.product_id",
            "does_not_exist",
            "Topilmadi.",
        ),
        (
            {"link.kind": "product", "link.product_id": "abc"},
            "link.product_id",
            "invalid",
            "Noto‘g‘ri qiymat.",
        ),
        ({"link.kind": "category"}, "link.category", "required", "Majburiy maydon."),
        ({"link.kind": "category", "link.category": "nope"}, "link.category", "does_not_exist", "Topilmadi."),
        ({"is_active": "maybe"}, "is_active", "invalid", "Noto‘g‘ri qiymat."),
        ({"starts_at": "2026-11-01T00:00:00"}, "starts_at", "invalid", "Noto‘g‘ri qiymat."),
        ({"ends_at": "not-a-date"}, "ends_at", "invalid", "Noto‘g‘ri qiymat."),
        (
            {"starts_at": "2026-11-02T00:00:00+05:00", "ends_at": "2026-11-01T00:00:00+05:00"},
            "ends_at",
            "invalid",
            "Noto‘g‘ri qiymat.",
        ),
        (
            {"starts_at": "2026-11-01T00:00:00+05:00", "ends_at": "2026-11-01T00:00:00+05:00"},
            "ends_at",
            "invalid",
            "Noto‘g‘ri qiymat.",
        ),
    ],
)
def test_create_validation_table(
    staff_client: APIClient, overrides: dict[str, Any], field: str, code: str, message: str
) -> None:
    hidden = ProductFactory.create(status="hidden")
    body = form(**{"title.ru": "", "title.en": ""})
    for key, value in overrides.items():
        if key == "image" and value is None:
            body.pop("image")
        elif key == "image" and value == "spoof":
            body["image"] = upload(content=b"not an image", name="x.png")
        elif key == "image" and value == "huge":
            body["image"] = upload(content=b"\x89PNG" + b"\0" * (5 * 1024 * 1024 - 3), name="big.png")
        elif value == "HIDDEN":
            body[key] = str(hidden.pk)
        else:
            body[key] = value
    response = staff_client.post(BANNERS, body, format="multipart")
    assert response.status_code == 400, response.json()
    assert errors_of(response)[field] == (code, message)
    assert not Banner.objects.exists()


def test_create_requires_multipart(staff_client: APIClient) -> None:
    response = staff_client.post(BANNERS, {"title": {"uz": "x", "ru": "", "en": ""}}, format="json")
    assert response.status_code == 415 and response.json()["code"] == "unsupported_media_type"


def test_urlencoded_bodies_are_415(staff_client: APIClient) -> None:
    """Contract §1.2: JSON or multipart (where stated), anything else → 415 — form-urlencoded included."""
    banner = BannerFactory.create(title_uz="Kuz")
    for method, url in (("post", BANNERS), ("patch", f"{BANNERS}{banner.pk}/")):
        response = getattr(staff_client, method)(
            url, "title.uz=Changed", content_type="application/x-www-form-urlencoded"
        )
        assert response.status_code == 415, (method, response.content)
        assert response.json()["code"] == "unsupported_media_type"
    banner.refresh_from_db()
    assert banner.title_uz == "Kuz" and Banner.objects.count() == 1


def test_create_with_uz_only_title_defaults_other_languages_to_empty(staff_client: APIClient) -> None:
    """Contract §2.5: only ``title.uz`` is required; ``ru`` / ``en`` missing on create → ``""``."""
    response = staff_client.post(BANNERS, form(), format="multipart")
    assert response.status_code == 201, response.json()
    assert response.json()["title"] == {"uz": "Kuzgi chegirmalar", "ru": "", "en": ""}
    banner = Banner.objects.get()
    assert (banner.title_uz, banner.title_ru, banner.title_en) == ("Kuzgi chegirmalar", "", "")


def test_list_and_export(staff_client: APIClient) -> None:
    product = ProductFactory.create(status="active")
    category = CategoryFactory.create(slug="serums")
    second = BannerFactory.create(
        title_uz="Ikkinchi",
        position=1,
        link_kind="category",
        link_category=category,
        is_active=False,
        starts_at=datetime(2026, 11, 1, 0, 0, tzinfo=TASHKENT),
        ends_at=datetime(2026, 11, 30, 18, 30, tzinfo=TASHKENT),
    )
    first = BannerFactory.create(title_uz="+Birinchi", position=0, link_kind="product", link_product=product)
    response = staff_client.get(BANNERS)
    assert response.status_code == 200
    body = response.json()
    assert (body["count"], body["page_size"]) == (2, 20)
    assert [item["id"] for item in body["results"]] == [first.pk, second.pk]
    assert body["results"][0]["link"] == {"kind": "product", "product_id": product.pk, "category": None}
    assert body["results"][1]["link"] == {"kind": "category", "product_id": None, "category": "serums"}
    assert [item["id"] for item in staff_client.get(BANNERS, {"is_active": "false"}).json()["results"]] == [
        second.pk
    ]
    assert errors_of(staff_client.get(BANNERS, {"is_active": "x"}))["is_active"][0] == "invalid_choice"
    export = staff_client.get(BANNERS, {"export": "csv"})
    assert export.status_code == 200
    assert export["Content-Disposition"].startswith('attachment; filename="bannerlar-')
    rows = list(csv.reader(StringIO(export.content.decode("utf-8-sig")), delimiter=";"))
    assert rows == [
        ["ID", "Sarlavha", "Havola", "Faol", "Boshlanishi", "Tugashi", "Tartib"],
        [str(first.pk), "'+Birinchi", f"mahsulot #{product.pk}", "Ha", "", "", "0"],
        [
            str(second.pk),
            "Ikkinchi",
            "kategoriya serums",
            "Yo‘q",
            "2026-11-01 00:00",
            "2026-11-30 18:30",
            "1",
        ],
    ]
    assert (AuditEntry.objects.get(action="export.downloaded").after or {})["resource"] == "banners"
    assert staff_client.get(BANNERS, {"export": "xlsx"}).status_code == 200


def test_retrieve_and_404(staff_client: APIClient) -> None:
    banner = BannerFactory.create()
    response = staff_client.get(f"{BANNERS}{banner.pk}/")
    assert response.status_code == 200 and response.json()["id"] == banner.pk
    assert staff_client.get(f"{BANNERS}999999/").status_code == 404
    assert staff_client.patch(f"{BANNERS}999999/", {"is_active": False}).status_code == 404
    assert staff_client.delete(f"{BANNERS}999999/").status_code == 404


def test_patch_json_merges_and_validates_against_current_state(staff_client: APIClient) -> None:
    product = ProductFactory.create(status="active")
    banner = BannerFactory.create(
        title_uz="Kuz", title_ru="Осень", starts_at=datetime(2026, 11, 10, 0, 0, tzinfo=TASHKENT)
    )
    url = f"{BANNERS}{banner.pk}/"
    response = staff_client.patch(
        url, {"title": {"en": "Autumn"}, "link": {"kind": "product", "product_id": product.pk}}
    )
    assert response.status_code == 200, response.json()
    assert response.json()["title"] == {"uz": "Kuz", "ru": "Осень", "en": "Autumn"}
    assert response.json()["link"] == {"kind": "product", "product_id": product.pk, "category": None}
    entry = AuditEntry.objects.get(action="banner.updated")
    assert entry.before == {"title.en": "", "link.kind": "none", "link.product_id": None}
    assert entry.after == {"title.en": "Autumn", "link.kind": "product", "link.product_id": product.pk}
    # ends_at is checked against the stored starts_at
    response = staff_client.patch(url, {"ends_at": "2026-11-05T00:00:00+05:00"})
    assert errors_of(response)["ends_at"] == ("invalid", "Noto‘g‘ri qiymat.")
    response = staff_client.patch(url, {"ends_at": "2026-11-15T00:00:00+05:00"})
    assert response.status_code == 200 and response.json()["ends_at"] == "2026-11-15T00:00:00+05:00"
    # switching the kind without a target
    response = staff_client.patch(url, {"link": {"kind": "category"}})
    assert errors_of(response)["link.category"] == ("required", "Majburiy maydon.")
    # a hidden product is not linkable
    hidden = ProductFactory.create(status="hidden")
    response = staff_client.patch(url, {"link": {"product_id": hidden.pk}})
    assert errors_of(response)["link.product_id"] == ("does_not_exist", "Topilmadi.")
    # kind none clears the target; a later product_id without kind reuses the stored kind
    response = staff_client.patch(url, {"link": {"kind": "none"}})
    assert response.json()["link"] == {"kind": "none", "product_id": None, "category": None}
    assert Banner.objects.get(pk=banner.pk).link_product_id is None
    response = staff_client.patch(url, {"starts_at": None, "ends_at": None, "is_active": False})
    assert response.status_code == 200
    assert response.json()["starts_at"] is None and response.json()["is_active"] is False
    assert Banner.objects.get(pk=banner.pk).image.name == banner.image.name  # image untouched


def test_patch_multipart_replaces_image(
    staff_client: APIClient, django_capture_on_commit_callbacks: Any
) -> None:
    banner = BannerFactory.create(title_uz="Kuz")
    old_path = Path(banner.image.path)
    with django_capture_on_commit_callbacks(execute=True):
        response = staff_client.patch(
            f"{BANNERS}{banner.pk}/",
            {"image": upload("PNG", size=(40, 20)), "title.uz": "Qish", "is_active": "0"},
            format="multipart",
        )
    assert response.status_code == 200, response.json()
    banner.refresh_from_db()
    assert banner.title_uz == "Qish" and banner.is_active is False
    assert str(banner.image.name).endswith(".png") and (banner.image_width, banner.image_height) == (40, 20)
    assert response.json()["image_url"] == media_url(banner.image.name)
    assert Path(banner.image.path).exists() and not old_path.exists()
    response = staff_client.patch(
        f"{BANNERS}{banner.pk}/", {"image": upload(content=b"nope", name="x.png")}, format="multipart"
    )
    assert errors_of(response)["image"] == ("unsupported_image", "Faqat JPEG, PNG yoki WebP rasm.")


def test_delete_compacts_positions(staff_client: APIClient, django_capture_on_commit_callbacks: Any) -> None:
    first, second, third = (BannerFactory.create(position=position) for position in range(3))
    path = Path(second.image.path)
    with django_capture_on_commit_callbacks(execute=True):
        response = staff_client.delete(f"{BANNERS}{second.pk}/")
    assert response.status_code == 204
    assert not path.exists()
    assert list(Banner.objects.order_by("position").values_list("id", "position")) == [
        (first.pk, 0),
        (third.pk, 1),
    ]
    entry = AuditEntry.objects.get(action="banner.deleted")
    assert entry.entity_id == str(second.pk) and (entry.before or {})["position"] == 1


def test_reorder(staff_client: APIClient) -> None:
    a, b, c = (BannerFactory.create(position=position) for position in range(3))
    url = f"{BANNERS}order/"
    response = staff_client.put(url, {"ids": [c.pk, a.pk, b.pk]})
    assert response.status_code == 200, response.json()
    assert [(item["id"], item["position"]) for item in response.json()["results"]] == [
        (c.pk, 0),
        (a.pk, 1),
        (b.pk, 2),
    ]
    assert set(response.json()["results"][0]) == {
        "id",
        "title",
        "image_url",
        "link",
        "is_active",
        "starts_at",
        "ends_at",
        "position",
        "created_at",
        "updated_at",
    }
    entry = AuditEntry.objects.get(action="banner.reordered")
    assert entry.before == {"ids": [a.pk, b.pk, c.pk]} and entry.after == {"ids": [c.pk, a.pk, b.pk]}
    for body in ([a.pk, b.pk], [a.pk, b.pk, c.pk, 999999], [a.pk, a.pk, b.pk]):
        response = staff_client.put(url, {"ids": body})
        assert response.status_code == 400, body
        assert errors_of(response)["ids"] == ("mismatch", "Ro‘yxat joriy elementlarga mos emas.")
    assert errors_of(staff_client.put(url, {}))["ids"] == ("required", "Majburiy maydon.")
    assert errors_of(staff_client.put(url, {"ids": []}))["ids"] == (
        "mismatch",
        "Ro‘yxat joriy elementlarga mos emas.",
    )
    assert staff_client.get(url).status_code == 405


def test_reorder_empty_list_without_banners_is_a_noop(staff_client: APIClient) -> None:
    response = staff_client.put(f"{BANNERS}order/", {"ids": []})
    assert response.status_code == 200, response.json()
    assert response.json() == {"results": []}
