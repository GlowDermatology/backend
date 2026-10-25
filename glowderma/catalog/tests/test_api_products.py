"""Contract §2.3 ``products/``: list, stats, create, retrieve, PATCH (validation table, shapes, audit)."""

import csv
from datetime import datetime
from io import BytesIO, StringIO
from typing import Any

import pytest
from django.conf import settings
from openpyxl import load_workbook  # type: ignore[import-untyped]
from rest_framework.test import APIClient

from glowderma.accounts.models import StaffUser
from glowderma.catalog.models import Product, ProductStock
from glowderma.catalog.tests.factories import (
    CategoryFactory,
    ProductFactory,
    ProductImageFactory,
    ProductStockFactory,
)
from glowderma.catalog.tests.helpers import errors_of
from glowderma.core import enums, timeutils
from glowderma.core.models import AuditEntry
from glowderma.core.timeutils import TASHKENT

pytestmark = pytest.mark.django_db

PRODUCTS = "/api/v1/admin/products/"


def ids(response: Any) -> list[int]:
    return [item["id"] for item in response.json()["results"]]


# --- list ---------------------------------------------------------------------------------------------------


def test_list_envelope_and_default_page_size_eight(staff_client: APIClient) -> None:
    for _ in range(10):
        ProductFactory.create()
    response = staff_client.get(PRODUCTS)
    assert response.status_code == 200
    body = response.json()
    assert {key: body[key] for key in ("count", "page", "page_size", "page_count")} == {
        "count": 10,
        "page": 1,
        "page_size": 8,
        "page_count": 2,
    }
    assert len(body["results"]) == 8
    assert staff_client.get(PRODUCTS, {"page": 2}).json()["page"] == 2
    assert staff_client.get(PRODUCTS, {"page": 99}).json()["page"] == 2  # clamped, never 404
    assert staff_client.get(PRODUCTS, {"page_size": 8}).status_code == 200


@pytest.mark.parametrize("size", ["20", "6", "abc", "0"])
def test_page_size_outside_allow_list(staff_client: APIClient, size: str) -> None:
    response = staff_client.get(PRODUCTS, {"page_size": size})
    assert response.status_code == 400
    assert errors_of(response)["page_size"] == ("invalid_choice", "Ruxsat etilmagan qiymat.")


def test_list_item_shape(staff_client: APIClient, staff: StaffUser) -> None:
    category = CategoryFactory.create(slug="serums")
    product = ProductFactory.create(
        billz_id="4607012301",
        billz_name="HYALURONIC SERUM 30ML",
        category=category,
        name_uz="Gialuron kislotali serum",
        name_ru="Сыворотка",
        last_change_at=datetime(2026, 10, 22, 18, 4, tzinfo=TASHKENT),
        last_change_kind=enums.ProductChangeKind.PRICE_CHANGED,
        last_change_old_price=175_000,
        last_change_new_price=185_000,
        last_change_actor=staff,
    )
    ProductStockFactory.create(product=product, quantity=12)
    image = ProductImageFactory.create(product=product)
    item = staff_client.get(PRODUCTS).json()["results"][0]
    url = f"{settings.PUBLIC_MEDIA_BASE_URL.rstrip('/')}/{image.file.name}"
    assert item == {
        "id": product.pk,
        "billz_id": "4607012301",
        "billz_name": "HYALURONIC SERUM 30ML",
        "volume": "30 ml",
        "category": "serums",
        "price": 185000,
        "billz_price": 185000,
        "stock": 12,
        "promo": False,
        "status": "active",
        "is_new_from_billz": False,
        "texts": {
            "uz": {"name": "Gialuron kislotali serum", "description": "", "ingredients": "", "usage": ""},
            "ru": {"name": "Сыворотка", "description": "", "ingredients": "", "usage": ""},
            "en": {"name": "", "description": "", "ingredients": "", "usage": ""},
        },
        "main_image_url": url,
        "images": [{"id": image.pk, "url": url, "position": 0}],
        "last_change": {
            "at": "2026-10-22T18:04:00+05:00",
            "actor": {"id": staff.pk, "display_name": staff.full_name},
            "kind": "price_changed",
            "old_price": 175000,
            "new_price": 185000,
        },
        "created_at": timeutils.isoformat(product.created_at),
        "updated_at": timeutils.isoformat(product.updated_at),
    }
    assert url.startswith("http://127.0.0.1:8000/media/products/")


def test_list_nulls_for_never_edited_product_without_stock_row(staff_client: APIClient) -> None:
    product = ProductFactory.create(category=None, billz_price=None)
    item = staff_client.get(PRODUCTS).json()["results"][0]
    assert item["id"] == product.pk
    assert item["category"] is None and item["billz_price"] is None
    assert item["stock"] == 0 and item["main_image_url"] is None and item["images"] == []
    assert item["last_change"] is None


def test_search_vectors(staff_client: APIClient) -> None:
    serum = ProductFactory.create(
        id=4607,
        name_uz="Gialuron kislotali serum",
        name_ru="Сыворотка",
        name_en="Hyaluronic",
        billz_id="4607012301",
    )
    spf = ProductFactory.create(
        id=77, name_uz="Quyoshdan himoya", billz_name="SUN CREAM SPF50", billz_id="4607012401"
    )

    def found(term: str) -> set[int]:
        response = staff_client.get(PRODUCTS, {"search": term})
        assert response.status_code == 200, response.json()
        return set(ids(response))

    assert found("SERUM") == {serum.pk}
    assert found("сывор") == {serum.pk}
    assert found("hyalur") == {serum.pk}
    assert found("sun cream") == {spf.pk}
    assert found("4607012401") == {spf.pk}
    assert found("77") == {spf.pk}  # numeric term matches the id exactly
    assert found("4607") == {serum.pk, spf.pk}  # id 4607 and every billz_id prefix
    assert found("²") == set()  # str.isdigit() but not int(): must not crash
    assert found("") == {serum.pk, spf.pk}
    assert found("x" * 80) == set()  # trimmed to 64, never an error


def test_category_filter(staff_client: APIClient) -> None:
    serums = CategoryFactory.create(slug="serums")
    in_serums = ProductFactory.create(category=serums)
    orphan = ProductFactory.create(category=None)
    ProductFactory.create()
    assert ids(staff_client.get(PRODUCTS, {"category": "serums"})) == [in_serums.pk]
    assert ids(staff_client.get(PRODUCTS, {"category": "none"})) == [orphan.pk]
    response = staff_client.get(PRODUCTS, {"category": "unknown"})
    assert response.status_code == 400
    assert errors_of(response)["category"] == ("invalid_choice", "Ruxsat etilmagan qiymat.")


def test_status_promo_stock_and_new_filters(staff_client: APIClient) -> None:
    active = ProductFactory.create(status="active", promo=True)
    ProductStockFactory.create(product=active, quantity=3)
    hidden = ProductFactory.create(status="hidden", is_new_from_billz=True)
    ProductStockFactory.create(product=hidden, quantity=0)
    no_stock_row = ProductFactory.create(status="hidden")
    assert ids(staff_client.get(PRODUCTS, {"status": "active"})) == [active.pk]
    assert set(ids(staff_client.get(PRODUCTS, {"status": "hidden"}))) == {hidden.pk, no_stock_row.pk}
    assert ids(staff_client.get(PRODUCTS, {"promo": "true"})) == [active.pk]
    assert set(ids(staff_client.get(PRODUCTS, {"promo": "0"}))) == {hidden.pk, no_stock_row.pk}
    assert set(ids(staff_client.get(PRODUCTS, {"out_of_stock": "1"}))) == {hidden.pk, no_stock_row.pk}
    assert ids(staff_client.get(PRODUCTS, {"out_of_stock": "false"})) == [active.pk]
    assert ids(staff_client.get(PRODUCTS, {"is_new": "true"})) == [hidden.pk]
    for param in ("status", "promo", "out_of_stock", "is_new", "ordering"):
        response = staff_client.get(PRODUCTS, {param: "maybe"})
        assert response.status_code == 400, param
        assert errors_of(response)[param] == ("invalid_choice", "Ruxsat etilmagan qiymat."), param


def test_ordering_by_name_uses_the_ci_collation(staff_client: APIClient) -> None:
    names = [
        "Yulduz serum",
        "ыы krem",
        "Ёлка",
        "ёлка",
        "aloe",
        "Aloe vera",
        "o‘rik",
        "Orik",
        "O'rik",
        "",
        "Zebra",
    ]
    for name in names:
        ProductFactory.create(name_uz=name, billz_name="BAKUCHIOL BALM" if name == "" else "X")
    expected = [
        "aloe",
        "Aloe vera",
        "BAKUCHIOL BALM",  # empty uz name: the display name falls back to billz_name
        "O'rik",
        "o‘rik",
        "Orik",
        "Yulduz serum",
        "Zebra",
        "Ёлка",
        "ёлка",
        "ыы krem",
    ]

    def labels(ordering: str) -> list[str]:
        response = staff_client.get(PRODUCTS, {"ordering": ordering, "page_size": "8"})
        first = response.json()["results"]
        second = staff_client.get(PRODUCTS, {"ordering": ordering, "page": "2"}).json()["results"]
        return [item["texts"]["uz"]["name"] or item["billz_name"] for item in first + second]

    assert labels("name") == expected
    assert labels("-name") == expected[::-1]


def test_default_ordering_is_last_change_then_created_then_id(staff_client: APIClient) -> None:
    def day(value: int) -> datetime:
        return datetime(2026, 10, value, 9, 0, tzinfo=TASHKENT)

    edited = ProductFactory.create(created_at=day(1), last_change_at=day(20), last_change_kind="updated")
    newer = ProductFactory.create(created_at=day(10))
    older = ProductFactory.create(created_at=day(5))
    same = ProductFactory.create(created_at=day(5))
    assert ids(staff_client.get(PRODUCTS)) == [edited.pk, newer.pk, same.pk, older.pk]
    assert ids(staff_client.get(PRODUCTS, {"ordering": "default"})) == [
        edited.pk,
        newer.pk,
        same.pk,
        older.pk,
    ]


# --- export -------------------------------------------------------------------------------------------------


def test_csv_export(staff_client: APIClient, staff: StaffUser) -> None:
    category = CategoryFactory.create(slug="serums", name_uz="Serumlar")
    product = ProductFactory.create(
        billz_id="4607012301",
        billz_name='=HYPERLINK("x")',
        name_uz="-Gialuron",
        category=category,
        price=185_000,
        promo=True,
        status="hidden",
        is_new_from_billz=True,
    )
    ProductStockFactory.create(product=product, quantity=12)
    response = staff_client.get(PRODUCTS, {"export": "csv", "status": "hidden", "page": "9"})
    assert response.status_code == 200
    assert response["Content-Type"] == "text/csv; charset=utf-8"
    assert response["Content-Disposition"].startswith('attachment; filename="mahsulotlar-')
    assert response["Content-Disposition"].endswith('.csv"')
    raw = response.content
    assert raw.startswith(b"\xef\xbb\xbf") and b"\r\n" in raw
    rows = list(csv.reader(StringIO(raw.decode("utf-8-sig")), delimiter=";"))
    assert rows[0] == [
        "ID",
        "Billz ID",
        "Billz nomi",
        "Nomi",
        "Kategoriya",
        "Narx (so‘m)",
        "Qoldiq",
        "Aksiya",
        "Holat",
        "Yangi",
    ]
    assert rows[1] == [
        str(product.pk),
        "4607012301",
        '\'=HYPERLINK("x")',
        "'-Gialuron",
        "Serumlar",
        "185000",
        "12",
        "Ha",
        "Yashirin",
        "Ha",
    ]
    entry = AuditEntry.objects.get(action="export.downloaded")
    assert entry.actor_id == staff.pk
    assert entry.after == {
        "resource": "products",
        "filters": {"status": "hidden"},
        "row_count": 1,
        "format": "csv",
    }


def test_xlsx_export(staff_client: APIClient) -> None:
    product = ProductFactory.create(name_uz="Serum", category=None, status="active")
    response = staff_client.get(PRODUCTS, {"export": "xlsx"})
    assert response.status_code == 200
    assert response["Content-Disposition"].startswith('attachment; filename="mahsulotlar-')
    workbook = load_workbook(BytesIO(response.content))
    sheet = workbook["mahsulotlar"]
    assert [cell.value for cell in sheet[1]][:4] == ["ID", "Billz ID", "Billz nomi", "Nomi"]
    row = [cell for cell in sheet[2]]
    assert row[0].value == product.pk and row[0].data_type == "n"
    assert row[3].value == "Serum" and row[3].data_type == "s"
    assert row[4].value in ("", None) and row[5].value == 185_000 and row[8].value == "Faol"
    workbook.close()


def test_export_format_and_throttle(staff_client: APIClient) -> None:
    ProductFactory.create()
    response = staff_client.get(PRODUCTS, {"export": "pdf"})
    assert response.status_code == 400
    assert errors_of(response)["export"] == ("invalid_choice", "Ruxsat etilmagan qiymat.")
    for _ in range(10):
        assert staff_client.get(PRODUCTS, {"export": "csv"}).status_code == 200
    throttled = staff_client.get(PRODUCTS, {"export": "csv"})
    assert throttled.status_code == 429
    assert throttled.json()["code"] == "throttled"
    assert throttled["Retry-After"]
    assert staff_client.get(PRODUCTS).status_code == 200  # the JSON list has its own (staff) scope


# --- stats --------------------------------------------------------------------------------------------------


def test_stats_counts_and_billz_block(staff_client: APIClient) -> None:
    active = ProductFactory.create(status="active")
    ProductStockFactory.create(product=active, quantity=0)
    ProductFactory.create(status="hidden", is_new_from_billz=True)  # no stock row → out of stock
    stocked = ProductFactory.create(status="active")
    ProductStockFactory.create(product=stocked, quantity=5)
    response = staff_client.get(f"{PRODUCTS}stats/")
    assert response.status_code == 200
    assert response.json() == {
        "total": 3,
        "active": 2,
        "hidden": 1,
        "new_from_billz": 1,
        "out_of_stock": 2,
        "billz": {"connected": True, "last_sync_at": None},
    }


# --- retrieve -----------------------------------------------------------------------------------------------


def test_retrieve_and_404(staff_client: APIClient) -> None:
    product = ProductFactory.create()
    response = staff_client.get(f"{PRODUCTS}{product.pk}/")
    assert response.status_code == 200 and response.json()["id"] == product.pk
    missing = staff_client.get(f"{PRODUCTS}999999/")
    assert missing.status_code == 404
    assert missing.json() == {
        "code": "not_found",
        "message": "Topilmadi.",
        "errors": [],
        "request_id": missing.json()["request_id"],
    }


# --- create -------------------------------------------------------------------------------------------------


def valid_body(**overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "billz_id": "4607012301",
        "texts": {"uz": {"name": "Gialuron kislotali serum"}},
        "price": 185_000,
    }
    body.update(overrides)
    return body


def test_create_happy_path_full_shape(staff_client: APIClient, staff: StaffUser) -> None:
    CategoryFactory.create(slug="serums")
    body = valid_body(
        texts={
            "uz": {
                "name": "  Gialuron kislotali serum ",
                "description": "Tavsif",
                "ingredients": "Aqua",
                "usage": "Kuniga",
            },
            "ru": {"name": "Сыворотка"},
        },
        category="serums",
        promo=True,
    )
    response = staff_client.post(PRODUCTS, body)
    assert response.status_code == 201, response.json()
    product = Product.objects.get(billz_id="4607012301")
    stock = ProductStock.objects.get(product=product)
    data = response.json()
    assert data == {
        "id": product.pk,
        "billz_id": "4607012301",
        "billz_name": "BILLZ ITEM 4607012301",  # fake Billz synthetic product
        "volume": "50 ml",
        "category": "serums",
        "price": 185000,
        "billz_price": 50_000 + (4607012301 % 400) * 1_000,
        "stock": 4607012301 % 37,
        "promo": True,
        "status": "hidden",
        "is_new_from_billz": False,
        "texts": {
            "uz": {
                "name": "Gialuron kislotali serum",
                "description": "Tavsif",
                "ingredients": "Aqua",
                "usage": "Kuniga",
            },
            "ru": {"name": "Сыворотка", "description": "", "ingredients": "", "usage": ""},
            "en": {"name": "", "description": "", "ingredients": "", "usage": ""},
        },
        "main_image_url": None,
        "images": [],
        "last_change": {
            "at": timeutils.isoformat(product.last_change_at),  # type: ignore[arg-type]
            "actor": {"id": staff.pk, "display_name": staff.full_name},
            "kind": "created",
            "old_price": None,
            "new_price": None,
        },
        "created_at": timeutils.isoformat(product.created_at),
        "updated_at": timeutils.isoformat(product.updated_at),
    }
    assert stock.quantity == 4607012301 % 37 and stock.synced_at is not None
    entry = AuditEntry.objects.get(action="product.created")
    assert entry.entity_type == "product" and entry.entity_id == str(product.pk)
    assert entry.actor_id == staff.pk and entry.before is None
    assert (entry.after or {})["texts.uz.name"] == "Gialuron kislotali serum" and (entry.after or {})[
        "category"
    ] == "serums"


def test_create_defaults_status_hidden_and_promo_false(staff_client: APIClient) -> None:
    response = staff_client.post(PRODUCTS, valid_body())
    assert response.status_code == 201
    assert response.json()["status"] == "hidden" and response.json()["promo"] is False


@pytest.mark.parametrize(
    ("overrides", "field", "code", "message"),
    [
        (
            {"billz_id": "12345"},
            "billz_id",
            "invalid",
            "Billz ID yoki shtrix-kod 6–14 ta raqamdan iborat bo‘lsin.",
        ),
        (
            {"billz_id": "4607012301x"},
            "billz_id",
            "invalid",
            "Billz ID yoki shtrix-kod 6–14 ta raqamdan iborat bo‘lsin.",
        ),
        ({"billz_id": "4607010000"}, "billz_id", "billz_not_found", "Billz’da bunday mahsulot topilmadi."),
        (
            {"texts": {"uz": {"name": "G"}}},
            "texts.uz.name",
            "min_length",
            "O‘zbekcha nomni kiriting (kamida 2 belgi).",
        ),
        (
            {"texts": {"uz": {"name": "   "}}},
            "texts.uz.name",
            "min_length",
            "O‘zbekcha nomni kiriting (kamida 2 belgi).",
        ),
        ({"texts": {"uz": {"name": "x" * 121}}}, "texts.uz.name", "max_length", "120 belgidan oshmasin."),
        (
            {"texts": {"uz": {"name": "Serum", "description": "x" * 2001}}},
            "texts.uz.description",
            "max_length",
            "2000 belgidan oshmasin.",
        ),
        (
            {"texts": {"uz": {"name": "Serum", "ingredients": "x" * 1001}}},
            "texts.uz.ingredients",
            "max_length",
            "1000 belgidan oshmasin.",
        ),
        (
            {"texts": {"uz": {"name": "Serum", "usage": "x" * 1001}}},
            "texts.uz.usage",
            "max_length",
            "1000 belgidan oshmasin.",
        ),
        (
            {"texts": {"uz": {"name": "Serum"}, "ru": {"name": "x" * 121}}},
            "texts.ru.name",
            "max_length",
            "120 belgidan oshmasin.",
        ),
        (
            {"texts": {"uz": {"name": "Serum"}, "en": {"name": "x" * 121}}},
            "texts.en.name",
            "max_length",
            "120 belgidan oshmasin.",
        ),
        ({"price": 999}, "price", "min_value", "Narx kamida 1 000 so‘m bo‘lsin."),
        ({"price": 100_000_001}, "price", "max_value", "Narx juda katta."),
        ({"price": "abc"}, "price", "invalid", "Noto‘g‘ri qiymat."),
        ({"category": "nope"}, "category", "does_not_exist", "Topilmadi."),
        ({"status": "draft"}, "status", "invalid_choice", "Ruxsat etilmagan qiymat."),
        ({"promo": "maybe"}, "promo", "invalid", "Noto‘g‘ri qiymat."),
    ],
)
def test_create_validation_table(
    staff_client: APIClient, overrides: dict[str, Any], field: str, code: str, message: str
) -> None:
    response = staff_client.post(PRODUCTS, valid_body(**overrides))
    assert response.status_code == 400, response.json()
    assert errors_of(response)[field] == (code, message)
    assert not Product.objects.exists()


def test_create_required_fields(staff_client: APIClient) -> None:
    response = staff_client.post(PRODUCTS, {})
    assert response.status_code == 400
    errors = errors_of(response)
    assert errors["billz_id"] == ("required", "Majburiy maydon.")
    assert errors["texts"] == ("required", "Majburiy maydon.")
    assert errors["price"] == ("required", "Majburiy maydon.")
    response = staff_client.post(PRODUCTS, valid_body(texts={"ru": {"name": "x"}}))
    assert errors_of(response)["texts.uz"] == ("required", "Majburiy maydon.")
    response = staff_client.post(PRODUCTS, valid_body(texts={"uz": {"description": "x"}}))
    assert errors_of(response)["texts.uz.name"] == ("required", "Majburiy maydon.")


def test_create_unique_billz_id(staff_client: APIClient) -> None:
    ProductFactory.create(billz_id="4607012301")
    response = staff_client.post(PRODUCTS, valid_body())
    assert response.status_code == 400
    assert errors_of(response)["billz_id"] == ("unique", "Bu Billz ID bilan mahsulot allaqachon bor.")


def test_create_active_always_fails(staff_client: APIClient) -> None:
    CategoryFactory.create(slug="serums")
    response = staff_client.post(PRODUCTS, valid_body(status="active", category="serums"))
    assert response.status_code == 400
    assert errors_of(response) == {
        "images": ("required_for_active", "Faol mahsulotda kamida bitta rasm bo‘lsin.")
    }
    response = staff_client.post(PRODUCTS, valid_body(status="active"))
    assert errors_of(response) == {
        "category": ("required_for_active", "Faol mahsulot uchun kategoriyani tanlang."),
        "images": ("required_for_active", "Faol mahsulotda kamida bitta rasm bo‘lsin."),
    }
    assert not Product.objects.exists() and not AuditEntry.objects.filter(action="product.created").exists()


def test_create_rejects_non_json(staff_client: APIClient) -> None:
    response = staff_client.post(PRODUCTS, {"billz_id": "4607012301"}, format="multipart")
    assert response.status_code == 415
    assert response.json()["code"] == "unsupported_media_type"


def test_idempotency_key_is_honoured_on_create(staff_client: APIClient) -> None:
    key = "6f1c2a0e-9b8d-4c37-8a1e-000000000001"
    first = staff_client.post(PRODUCTS, valid_body(), HTTP_IDEMPOTENCY_KEY=key)
    assert first.status_code == 201
    replay = staff_client.post(PRODUCTS, valid_body(), HTTP_IDEMPOTENCY_KEY=key)
    assert replay.status_code == 201 and replay["Idempotent-Replayed"] == "true"
    assert replay.json() == first.json() and Product.objects.count() == 1
    reused = staff_client.post(PRODUCTS, valid_body(price=200_000), HTTP_IDEMPOTENCY_KEY=key)
    assert reused.status_code == 422 and reused.json()["code"] == "idempotency_key_reused"


# --- PATCH --------------------------------------------------------------------------------------------------


def detail(product: Product) -> str:
    return f"{PRODUCTS}{product.pk}/"


def test_patch_merges_texts_at_leaf_level(staff_client: APIClient) -> None:
    product = ProductFactory.create(
        name_uz="Serum",
        description_uz="Tavsif",
        name_ru="Сыворотка",
        usage_ru="Утром",
        is_new_from_billz=True,
        status="hidden",
    )
    response = staff_client.patch(
        detail(product), {"texts": {"ru": {"name": "Новая"}, "en": {"usage": "AM"}}}
    )
    assert response.status_code == 200, response.json()
    assert response.json()["texts"] == {
        "uz": {"name": "Serum", "description": "Tavsif", "ingredients": "", "usage": ""},
        "ru": {"name": "Новая", "description": "", "ingredients": "", "usage": "Утром"},
        "en": {"name": "", "description": "", "ingredients": "", "usage": "AM"},
    }
    assert response.json()["is_new_from_billz"] is False
    assert response.json()["last_change"]["kind"] == "updated"
    entry = AuditEntry.objects.get(action="product.updated")
    assert entry.before == {"texts.ru.name": "Сыворотка", "texts.en.usage": ""}
    assert entry.after == {"texts.ru.name": "Новая", "texts.en.usage": "AM"}


def test_patch_validates_uz_name_when_sent(staff_client: APIClient) -> None:
    product = ProductFactory.create(status="hidden")
    response = staff_client.patch(detail(product), {"texts": {"uz": {"name": "x"}}})
    assert response.status_code == 400
    assert errors_of(response)["texts.uz.name"] == (
        "min_length",
        "O‘zbekcha nomni kiriting (kamida 2 belgi).",
    )
    response = staff_client.patch(detail(product), {"texts": {"uz": {"description": "ok"}}})
    assert response.status_code == 200  # uz.name is only mandatory on create


def test_patch_no_put_and_404(staff_client: APIClient) -> None:
    product = ProductFactory.create()
    assert staff_client.put(detail(product), {"price": 2000}).status_code == 405
    assert staff_client.patch(f"{PRODUCTS}999999/", {"price": 2000}).status_code == 404


@pytest.mark.parametrize(
    ("before", "body", "kind", "old_price", "new_price"),
    [
        (
            {"price": 100_000, "status": "hidden", "promo": False},
            {"price": 120_000, "status": "active", "promo": True},
            "price_changed",
            100_000,
            120_000,
        ),
        (
            {"price": 100_000, "status": "hidden", "promo": False},
            {"status": "active", "promo": True},
            "activated",
            None,
            None,
        ),
        (
            {"price": 100_000, "status": "active", "promo": True},
            {"status": "hidden", "promo": False},
            "hidden",
            None,
            None,
        ),
        (
            {"price": 100_000, "status": "active", "promo": False},
            {"promo": True, "texts": {"uz": {"name": "Yangi"}}},
            "promo_added",
            None,
            None,
        ),
        (
            {"price": 100_000, "status": "active", "promo": True},
            {"promo": False},
            "promo_removed",
            None,
            None,
        ),
        (
            {"price": 100_000, "status": "active", "promo": True},
            {"texts": {"en": {"name": "New"}}},
            "updated",
            None,
            None,
        ),
        ({"price": 100_000, "status": "active", "promo": True}, {"price": 100_000}, "updated", None, None),
    ],
)
def test_last_change_priority(
    staff_client: APIClient,
    staff: StaffUser,
    before: dict[str, Any],
    body: dict[str, Any],
    kind: str,
    old_price: int | None,
    new_price: int | None,
) -> None:
    product = ProductFactory.create(**before)
    ProductImageFactory.create(product=product)
    response = staff_client.patch(detail(product), body)
    assert response.status_code == 200, response.json()
    change = response.json()["last_change"]
    assert change["kind"] == kind
    assert (change["old_price"], change["new_price"]) == (old_price, new_price)
    assert change["actor"] == {"id": staff.pk, "display_name": staff.full_name}
    assert change["at"] == timeutils.isoformat(Product.objects.get(pk=product.pk).last_change_at)  # type: ignore[arg-type]


def test_patch_active_rule(staff_client: APIClient) -> None:
    product = ProductFactory.create(status="hidden", category=None)
    response = staff_client.patch(detail(product), {"status": "active"})
    assert response.status_code == 400
    assert errors_of(response) == {
        "category": ("required_for_active", "Faol mahsulot uchun kategoriyani tanlang."),
        "images": ("required_for_active", "Faol mahsulotda kamida bitta rasm bo‘lsin."),
    }
    assert Product.objects.get(pk=product.pk).status == "hidden"
    category = CategoryFactory.create(slug="serums")
    response = staff_client.patch(detail(product), {"status": "active", "category": "serums"})
    assert errors_of(response) == {
        "images": ("required_for_active", "Faol mahsulotda kamida bitta rasm bo‘lsin.")
    }
    ProductImageFactory.create(product=product)
    response = staff_client.patch(detail(product), {"status": "active", "category": category.slug})
    assert response.status_code == 200 and response.json()["status"] == "active"
    # clearing the category of an active product is refused too
    response = staff_client.patch(detail(product), {"category": None})
    assert errors_of(response) == {
        "category": ("required_for_active", "Faol mahsulot uchun kategoriyani tanlang.")
    }


def test_patch_activating_an_imported_product_requires_a_price(staff_client: APIClient) -> None:
    product = ProductFactory.create(status="hidden", price=0, is_new_from_billz=True)
    ProductImageFactory.create(product=product)
    response = staff_client.patch(detail(product), {"status": "active"})
    assert response.status_code == 400
    assert errors_of(response) == {"price": ("min_value", "Narx kamida 1 000 so‘m bo‘lsin.")}


def test_patch_category_null_and_unknown(staff_client: APIClient) -> None:
    product = ProductFactory.create(status="hidden")
    response = staff_client.patch(detail(product), {"category": None})
    assert response.status_code == 200 and response.json()["category"] is None
    response = staff_client.patch(detail(product), {"category": "nope"})
    assert errors_of(response)["category"] == ("does_not_exist", "Topilmadi.")


def test_patch_billz_id_refreshes_billz_fields_and_stock(staff_client: APIClient) -> None:
    product = ProductFactory.create(billz_id="4607012301", billz_name="OLD", status="hidden")
    ProductStockFactory.create(product=product, quantity=1)
    other = ProductFactory.create(billz_id="4607012302")
    response = staff_client.patch(detail(product), {"billz_id": other.billz_id})
    assert errors_of(response)["billz_id"] == ("unique", "Bu Billz ID bilan mahsulot allaqachon bor.")
    response = staff_client.patch(detail(product), {"billz_id": "4607010000"})
    assert errors_of(response)["billz_id"] == ("billz_not_found", "Billz’da bunday mahsulot topilmadi.")
    response = staff_client.patch(detail(product), {"billz_id": "4607012301"})  # unchanged: no refresh
    assert response.status_code == 200 and response.json()["billz_name"] == "OLD"
    response = staff_client.patch(detail(product), {"billz_id": "4607012399"})
    assert response.status_code == 200, response.json()
    data = response.json()
    assert data["billz_id"] == "4607012399" and data["billz_name"] == "BILLZ ITEM 4607012399"
    assert data["volume"] == "50 ml" and data["billz_price"] == 50_000 + (4607012399 % 400) * 1_000
    assert data["stock"] == 4607012399 % 37
    entry = AuditEntry.objects.filter(action="product.updated").latest("id")
    assert entry.before == {"billz_id": "4607012301"} and entry.after == {"billz_id": "4607012399"}


def test_patch_price_bounds_and_updated_at(staff_client: APIClient) -> None:
    product = ProductFactory.create(status="hidden")
    before = Product.objects.get(pk=product.pk).updated_at
    response = staff_client.patch(detail(product), {"price": 500})
    assert errors_of(response)["price"] == ("min_value", "Narx kamida 1 000 so‘m bo‘lsin.")
    response = staff_client.patch(detail(product), {"price": 1_000})
    assert response.status_code == 200 and response.json()["price"] == 1000
    assert Product.objects.get(pk=product.pk).updated_at >= before
