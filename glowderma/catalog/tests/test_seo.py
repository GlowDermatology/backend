"""B-W1 SEO data: product and specialist slugs (derived, unique, editable, looked up by the website), the
per-language ``seo`` texts of products and categories, the «SEO holati» checklist and ``updated_at``."""

import threading
from typing import Any

import pytest
from django.conf import settings
from django.db import connection
from rest_framework.test import APIClient

from glowderma.catalog import seo
from glowderma.catalog.models import Category, Product
from glowderma.catalog.tests.factories import (
    CategoryFactory,
    ProductFactory,
    ProductImageFactory,
    ProductStockFactory,
)
from glowderma.core import enums
from glowderma.core.models import AuditEntry
from glowderma.professionals.models import Professional
from glowderma.professionals.tests.factories import ProfessionalFactory

pytestmark = pytest.mark.django_db

PRODUCTS = "/api/v1/admin/products/"
CATEGORIES = "/api/v1/admin/categories/"


# --- slugs --------------------------------------------------------------------------------------------------


def test_a_new_product_gets_a_slug_from_its_uzbek_name_then_the_billz_name() -> None:
    first = ProductFactory.create(name_uz="Ko‘pikli yuvish geli")
    second = ProductFactory.create(name_uz="Ko'pikli yuvish geli")
    billz_only = ProductFactory.create(name_uz="", billz_name="СЫВОРОТКА 30МЛ")
    nothing = ProductFactory.create(name_uz="", billz_name="", billz_id="46071234567")
    assert first.slug == "kopikli-yuvish-geli"
    assert second.slug == "kopikli-yuvish-geli-2"
    assert billz_only.slug == "syvorotka-30ml"
    assert nothing.slug == "mahsulot-46071234567"


def test_a_slug_never_changes_on_its_own() -> None:
    product = ProductFactory.create(name_uz="Serum")
    product.name_uz = "Boshqa nom"
    product.save()
    product.refresh_from_db()
    assert product.slug == "serum"


def test_digits_only_and_malformed_slugs_are_refused_by_the_database() -> None:
    from django.db import IntegrityError, transaction  # noqa: PLC0415

    product = ProductFactory.create()
    for bad in ("12345", "Bad Slug", "a--b"):
        with pytest.raises(IntegrityError), transaction.atomic():
            Product.objects.filter(pk=product.pk).update(slug=bad)


@pytest.mark.django_db(transaction=True)
def test_two_concurrent_products_with_one_name_get_distinct_slugs() -> None:
    gate = threading.Event()
    errors: list[BaseException] = []

    def create(billz_id: str) -> None:
        try:
            gate.wait(10)
            ProductFactory.create(name_uz="Bir xil nom", billz_id=billz_id, category=None)
        except BaseException as exc:  # pragma: no cover - surfaced below
            errors.append(exc)
        finally:
            connection.close()

    threads = [threading.Thread(target=create, args=(f"46070000{n}0",)) for n in (1, 2, 3)]
    for thread in threads:
        thread.start()
    gate.set()
    for thread in threads:
        thread.join(10)
    assert errors == []
    assert sorted(Product.objects.values_list("slug", flat=True)) == [
        "bir-xil-nom",
        "bir-xil-nom-2",
        "bir-xil-nom-3",
    ]


def test_specialists_get_a_slug_too() -> None:
    first = ProfessionalFactory.create(name="Dr. Malika Karimova")
    second = ProfessionalFactory.create(name="Dr. Malika Karimova")
    cyrillic = ProfessionalFactory.create(name="Шахноза Юлдашева")
    assert (first.slug, second.slug) == ("dr-malika-karimova", "dr-malika-karimova-2")
    assert cyrillic.slug == "shaxnoza-yuldasheva"


# --- admin products -----------------------------------------------------------------------------------------


def test_admin_product_shows_and_edits_slug_and_seo(staff_client: APIClient) -> None:
    product = ProductFactory.create(name_uz="Gialuron serum", description_uz="", status="hidden")
    ProductStockFactory.create(product=product)
    response = staff_client.get(f"{PRODUCTS}{product.pk}/")
    body = response.json()
    assert body["slug"] == "gialuron-serum"
    assert body["seo"] == {language: {"title": "", "description": ""} for language in ("uz", "ru", "en")}
    codes = [issue["code"] for issue in body["seo_status"]["issues"]]
    assert body["seo_status"]["ok"] is False
    assert codes[:2] == ["description_missing", "images_missing"]  # errors first
    response = staff_client.patch(
        f"{PRODUCTS}{product.pk}/",
        {
            "slug": "gialuron-kislotali-serum",
            "seo": {
                "ru": {"title": "Сыворотка с гиалуроновой кислотой 30 мл"},
                "uz": {"description": "x" * 90},
            },
        },
    )
    assert response.status_code == 200, response.json()
    body = response.json()
    assert body["slug"] == "gialuron-kislotali-serum"
    assert body["seo"]["ru"] == {"title": "Сыворотка с гиалуроновой кислотой 30 мл", "description": ""}
    assert body["seo"]["uz"]["description"] == "x" * 90
    entry = AuditEntry.objects.filter(action="product.updated").latest("id")
    assert entry.before is not None and entry.after is not None
    assert entry.before["slug"] == "gialuron-serum" and entry.after["slug"] == "gialuron-kislotali-serum"
    assert entry.after["seo.ru.title"].startswith("Сыворотка")


@pytest.mark.parametrize(
    ("slug", "code"),
    [("12345", "invalid"), ("Upper-Case", "invalid"), ("a" * 141, "max_length"), ("taken-slug", "unique")],
)
def test_admin_product_slug_rules(staff_client: APIClient, slug: str, code: str) -> None:
    ProductFactory.create(slug="taken-slug")
    product = ProductFactory.create(status="hidden")
    response = staff_client.patch(f"{PRODUCTS}{product.pk}/", {"slug": slug})
    assert response.status_code == 400
    assert [(e["field"], e["code"]) for e in response.json()["errors"]] == [("slug", code)]


def test_admin_product_seo_limits(staff_client: APIClient) -> None:
    product = ProductFactory.create(status="hidden")
    response = staff_client.patch(
        f"{PRODUCTS}{product.pk}/", {"seo": {"en": {"title": "t" * 121, "description": "d" * 301}}}
    )
    assert response.status_code == 400
    fields = sorted(error["field"] for error in response.json()["errors"])
    assert fields == ["seo.en.description", "seo.en.title"]


def test_image_changes_move_the_product_updated_at(staff_client: APIClient) -> None:
    product = ProductFactory.create(status="hidden")
    before = Product.objects.get(pk=product.pk).updated_at
    image = ProductImageFactory.create(product=product)
    from glowderma.catalog import services  # noqa: PLC0415

    services.delete_image(product.pk, image.pk, actor=None)
    product.refresh_from_db()
    assert product.updated_at > before


# --- admin categories ---------------------------------------------------------------------------------------


def test_admin_category_seo_and_checklist(staff_client: APIClient) -> None:
    response = staff_client.post(
        CATEGORIES,
        {
            "slug": "serums",
            "name": {"uz": "Serumlar"},
            "seo": {"uz": {"title": "Serumlar — Glow Derma klinikasi"}},
        },
    )
    assert response.status_code == 201, response.json()
    body = response.json()
    assert body["seo"]["uz"] == {"title": "Serumlar — Glow Derma klinikasi", "description": ""}
    codes = {(issue["code"], issue["language"]) for issue in body["seo_status"]["issues"]}
    assert {
        ("name_translation_missing", "ru"),
        ("image_missing", None),
        ("no_active_products", None),
    } <= codes
    assert ("seo_title_missing", "uz") not in codes and ("seo_title_missing", "ru") in codes
    response = staff_client.patch(f"{CATEGORIES}serums/", {"seo": {"ru": {"description": "Короткое"}}})
    assert response.status_code == 200
    issues = response.json()["seo_status"]["issues"]
    assert {"code": "seo_description_short", "severity": "warning", "language": "ru"}.items() <= next(
        issue for issue in issues if issue["code"] == "seo_description_short"
    ).items()
    entry = AuditEntry.objects.filter(action="category.updated").latest("id")
    assert entry.after == {"seo.ru.description": "Короткое"}


def test_category_multipart_seo_fields(staff_client: APIClient) -> None:
    response = staff_client.post(
        CATEGORIES,
        {"slug": "creams", "name.uz": "Kremlar", "seo.en.title": "Creams for every skin"},
        format="multipart",
    )
    assert response.status_code == 201, response.json()
    assert Category.objects.get(slug="creams").seo_title_en == "Creams for every skin"


# --- the checklist itself -----------------------------------------------------------------------------------


def test_a_complete_product_is_ok() -> None:
    product = ProductFactory.create(
        name_uz="Gialuron serum",
        name_ru="Сыворотка",
        name_en="Serum",
        description_uz="u" * 100,
        description_ru="r" * 100,
        description_en="e" * 100,
        **{f"seo_title_{lang}": "A good page title here" for lang in ("uz", "ru", "en")},
        **{f"seo_description_{lang}": "d" * 120 for lang in ("uz", "ru", "en")},
    )
    issues = seo.product_issues(product, image_count=1)
    assert issues == []
    assert seo.status_payload(issues) == {"ok": True, "issues": []}


def test_checklist_bounds_and_hints() -> None:
    product = ProductFactory.create(
        name_uz="",
        description_uz="short",
        seo_title_uz="Short",
        seo_title_ru="t" * 61,
        seo_description_en="d" * 161,
        category=None,
    )
    issues = {
        (issue.code, issue.language): issue.severity for issue in seo.product_issues(product, image_count=2)
    }
    assert issues[("name_missing", "uz")] == "error"
    assert issues[("alt_text_fallback", "uz")] == "warning"
    assert issues[("description_short", "uz")] == "warning"
    assert issues[("seo_title_short", "uz")] == "warning"
    assert issues[("seo_title_long", "ru")] == "warning"
    assert issues[("seo_description_long", "en")] == "warning"
    assert issues[("seo_description_missing", "uz")] == "info"
    assert issues[("category_missing", None)] == "warning"
    only_hints = [seo.SeoIssue("seo_title_missing", "info", "uz", "…")]
    assert seo.status_payload(only_hints)["ok"] is True
    assert enums.SeoIssueCode.IMAGES_MISSING not in {code for code, _ in issues}


# --- customer side ------------------------------------------------------------------------------------------


@pytest.fixture
def web() -> APIClient:
    client = APIClient()
    client.credentials(
        HTTP_X_GD_DEVICE_ID="web-browser-cookie-0002",
        HTTP_X_GD_PLATFORM="web",
        HTTP_X_GD_INTERNAL_SECRET=settings.INTERNAL_API_SECRET,
    )
    return client


def test_customer_product_by_slug_with_seo_and_updated_at(web: APIClient) -> None:
    product = ProductFactory.create(
        name_uz="Gialuron serum", seo_title_ru="Сыворотка — купить", seo_description_ru="Описание"
    )
    ProductImageFactory.create(product=product)
    ProductStockFactory.create(product=product)
    by_slug = web.get("/api/v1/catalog/products/gialuron-serum/", HTTP_ACCEPT_LANGUAGE="ru")
    by_id = web.get(f"/api/v1/catalog/products/{product.pk}/", HTTP_ACCEPT_LANGUAGE="ru")
    assert by_slug.status_code == by_id.status_code == 200
    assert by_slug.json() == by_id.json()
    body = by_slug.json()
    assert body["slug"] == "gialuron-serum" and body["updated_at"]
    assert (body["seo_title"], body["seo_description"]) == ("Сыворотка — купить", "Описание")
    uz = web.get("/api/v1/catalog/products/gialuron-serum/", HTTP_ACCEPT_LANGUAGE="uz").json()
    assert (uz["seo_title"], uz["seo_description"]) == ("", "")  # never another language's text
    listing = web.get("/api/v1/catalog/products/").json()["results"]
    assert listing[0]["slug"] == "gialuron-serum" and "updated_at" in listing[0]
    assert web.get("/api/v1/catalog/products/no-such-product/").status_code == 404
    Product.objects.filter(pk=product.pk).update(status=enums.ProductStatus.HIDDEN)
    assert web.get("/api/v1/catalog/products/gialuron-serum/").status_code == 404


def test_customer_categories_carry_seo_and_updated_at(web: APIClient) -> None:
    category = CategoryFactory.create(seo_title_en="Serums", seo_description_en="All serums")
    product = ProductFactory.create(category=category)
    ProductImageFactory.create(product=product)
    rows = web.get("/api/v1/catalog/categories/", HTTP_ACCEPT_LANGUAGE="en").json()["results"]
    row = next(row for row in rows if row["slug"] == category.slug)
    assert (row["seo_title"], row["seo_description"]) == ("Serums", "All serums")
    assert row["updated_at"]


def test_customer_specialist_by_slug(web: APIClient, settings: Any) -> None:
    professional = ProfessionalFactory.create(name="Dr. Malika Karimova", is_active=True)
    by_slug = web.get("/api/v1/specialists/dr-malika-karimova/")
    by_id = web.get(f"/api/v1/specialists/{professional.pk}/")
    assert by_slug.status_code == by_id.status_code == 200
    assert by_slug.json() == by_id.json()
    assert by_slug.json()["slug"] == "dr-malika-karimova" and by_slug.json()["updated_at"]
    Professional.objects.filter(pk=professional.pk).update(is_active=False)
    assert web.get("/api/v1/specialists/dr-malika-karimova/").status_code == 404


def test_admin_specialist_slug_is_editable(staff_client: APIClient) -> None:
    professional = ProfessionalFactory.create(name="Aziza")
    ProfessionalFactory.create(name="Taken", slug="band-qilingan")
    url = f"/api/v1/admin/specialists/{professional.pk}/"
    assert staff_client.get(url).json()["slug"] == "aziza"
    taken = staff_client.patch(url, {"slug": "band-qilingan"})
    assert taken.status_code == 400 and taken.json()["errors"][0]["code"] == "unique"
    digits = staff_client.patch(url, {"slug": "2024"})
    assert digits.status_code == 400 and digits.json()["errors"][0]["code"] == "invalid"
    response = staff_client.patch(url, {"slug": "dr-aziza"})
    assert response.status_code == 200 and response.json()["slug"] == "dr-aziza"
    assert AuditEntry.objects.filter(action="professional.updated").latest("id").after == {"slug": "dr-aziza"}


def test_the_migrations_backfill_slugs_of_existing_rows() -> None:
    """``catalog.0005`` / ``professionals.0003``: rows from before B-W1 (and a previous release writing during
    a deploy, slug ``""``) get unique slugs; a later save of such a row derives one too."""
    import importlib  # noqa: PLC0415

    from django.apps import apps  # noqa: PLC0415

    first = ProductFactory.create(name_uz="Krem")
    second = ProductFactory.create(name_uz="Krem")
    numeric = ProductFactory.create(name_uz="", billz_name="", billz_id="46079999990")
    doctor = ProfessionalFactory.create(name="Aziza")
    Product.objects.filter(pk__in=[first.pk, second.pk, numeric.pk]).update(slug="")
    Professional.objects.filter(pk=doctor.pk).update(slug="")
    importlib.import_module("glowderma.catalog.migrations.0005_seo_slugs").backfill_product_slugs(apps, None)
    importlib.import_module("glowderma.professionals.migrations.0003_seo_slugs").backfill_professional_slugs(
        apps, None
    )
    slugs_by_pk = dict(Product.objects.values_list("pk", "slug"))
    assert (slugs_by_pk[first.pk], slugs_by_pk[second.pk]) == ("krem", "krem-2")
    assert slugs_by_pk[numeric.pk] == "mahsulot-46079999990"
    assert Professional.objects.get(pk=doctor.pk).slug == "aziza"
    Product.objects.filter(pk=first.pk).update(slug="")
    reloaded = Product.objects.get(pk=first.pk)
    reloaded.save()
    assert reloaded.slug == "krem"


# --- fix pass: digits-only names (adversarial review) ------------------------------------------------------


def test_digits_only_names_get_a_prefixed_slug() -> None:
    barcode = ProductFactory.create(name_uz="", billz_name="4607100012345")
    year = ProductFactory.create(name_uz="2024")
    agent = ProfessionalFactory.create(name="007")
    assert barcode.slug == "mahsulot-4607100012345"
    assert year.slug == "mahsulot-2024"
    assert agent.slug == "mutaxassis-007"


def test_the_billz_import_of_a_barcode_named_product() -> None:
    import dataclasses  # noqa: PLC0415

    from glowderma.catalog import services  # noqa: PLC0415
    from glowderma.integrations.billz.fake import FakeBillz  # noqa: PLC0415

    row = FakeBillz().get_product("4607100012345")
    assert row is not None
    assert services.upsert_from_billz(dataclasses.replace(row, billz_name="4607100012345")) == "created"
    product = Product.objects.get(billz_id="4607100012345")
    assert product.slug == "mahsulot-4607100012345"


def test_the_backfill_handles_digits_only_names() -> None:
    import importlib  # noqa: PLC0415

    from django.apps import apps  # noqa: PLC0415

    barcode = ProductFactory.create(name_uz="", billz_name="4607100099999")
    year = ProductFactory.create(name_uz="2024")
    agent = ProfessionalFactory.create(name="007")
    Product.objects.filter(pk__in=[barcode.pk, year.pk]).update(slug="")
    Professional.objects.filter(pk=agent.pk).update(slug="")
    importlib.import_module("glowderma.catalog.migrations.0005_seo_slugs").backfill_product_slugs(apps, None)
    importlib.import_module("glowderma.professionals.migrations.0003_seo_slugs").backfill_professional_slugs(
        apps, None
    )
    assert Product.objects.get(pk=barcode.pk).slug == "mahsulot-4607100099999"
    assert Product.objects.get(pk=year.pk).slug == "mahsulot-2024"
    assert Professional.objects.get(pk=agent.pk).slug == "mutaxassis-007"


def test_a_derived_slug_is_written_with_update_fields() -> None:
    product = ProductFactory.create(name_uz="Tonik")
    Product.objects.filter(pk=product.pk).update(slug="")
    reloaded = Product.objects.get(pk=product.pk)
    reloaded.price = 190_000
    reloaded.save(update_fields=["price"])
    assert Product.objects.get(pk=product.pk).slug == "tonik"
    professional = ProfessionalFactory.create(name="Aziza")
    Professional.objects.filter(pk=professional.pk).update(slug="")
    again = Professional.objects.get(pk=professional.pk)
    again.position = 9
    again.save(update_fields=["position"])
    assert Professional.objects.get(pk=professional.pk).slug == "aziza"
