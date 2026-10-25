"""catalog models: every CHECK / unique constraint, the generated search column, the deferrable position."""

from datetime import datetime
from typing import Any

import pytest
from django.db import IntegrityError, transaction

from glowderma.accounts.tests.factories import StaffUserFactory
from glowderma.catalog.models import Banner, Category, Product, ProductImage, ProductStock
from glowderma.catalog.tests.factories import (
    BannerFactory,
    CategoryFactory,
    ProductFactory,
    ProductImageFactory,
    ProductStockFactory,
)
from glowderma.core import enums
from glowderma.core.timeutils import TASHKENT

pytestmark = pytest.mark.django_db

T0 = datetime(2026, 10, 1, 10, 0, tzinfo=TASHKENT)
T1 = datetime(2026, 10, 2, 10, 0, tzinfo=TASHKENT)


def violates(constraint: str, create: Any, **kwargs: Any) -> None:
    with pytest.raises(IntegrityError, match=constraint), transaction.atomic():
        create(**kwargs)


# --- Category -----------------------------------------------------------------------------------------------


def test_category_str_ordering_and_defaults() -> None:
    second = CategoryFactory.create(slug="toners", name_uz="Tonerlar", position=1)
    first = CategoryFactory.create(slug="serums", name_uz="Serumlar", position=0)
    assert str(first) == "serums: Serumlar"
    assert list(Category.objects.all()) == [first, second]
    assert first.is_active and first.image.name == "" and first.name_ru == ""


@pytest.mark.parametrize("slug", ["A", "x", "Serums", "with space", "ser_ums"])
def test_category_slug_format(slug: str) -> None:
    violates("category_slug_format", CategoryFactory.create, slug=slug)


def test_category_slug_reserved_and_unique_and_name_required() -> None:
    violates("category_slug_not_reserved", CategoryFactory.create, slug="order")
    violates("category_name_uz_required", CategoryFactory.create, name_uz="")
    CategoryFactory.create(slug="serums")
    violates("catalog_category_slug_key", CategoryFactory.create, slug="serums")


# --- Product ------------------------------------------------------------------------------------------------


def test_product_str_display_name_and_default_ordering() -> None:
    named = ProductFactory.create(name_uz="Serum", billz_name="SERUM 30ML", billz_id="4607012301")
    unnamed = ProductFactory.create(name_uz="", billz_name="SPF 50", billz_id="4607012401")
    bare = ProductFactory.create(name_uz="", billz_name="", billz_id="4607012402")
    assert (str(named), str(unnamed), str(bare)) == ("Serum", "SPF 50", "4607012402")
    assert (named.display_name, unnamed.display_name, bare.display_name) == ("Serum", "SPF 50", "")
    assert list(Product.objects.all()) == [bare, unnamed, named]  # ordering = ["-id"]
    assert named.status == enums.ProductStatus.ACTIVE and Product().status == enums.ProductStatus.HIDDEN


@pytest.mark.parametrize("billz_id", ["12345", "46070123a1", "4607 012301", ""])
def test_product_billz_id_format(billz_id: str) -> None:
    violates("product_billz_id_format", ProductFactory.create, billz_id=billz_id)


def test_product_billz_id_unique_and_prices_non_negative() -> None:
    ProductFactory.create(billz_id="4607012301")
    violates("catalog_product_billz_id_key", ProductFactory.create, billz_id="4607012301")
    violates("product_price_non_negative", ProductFactory.create, price=-1)
    violates("product_billz_price_non_negative", ProductFactory.create, billz_price=-1)
    assert ProductFactory.create(price=0, billz_price=None).billz_price is None


def test_product_last_change_all_or_nothing() -> None:
    violates("product_last_change_all_or_nothing", ProductFactory.create, last_change_kind="updated")
    violates("product_last_change_all_or_nothing", ProductFactory.create, last_change_at=T0)
    violates("product_last_change_all_or_nothing", ProductFactory.create, last_change_old_price=1)
    violates(
        "product_last_change_all_or_nothing",
        ProductFactory.create,
        last_change_actor=StaffUserFactory.create(),
    )
    product = ProductFactory.create(
        last_change_at=T0, last_change_kind=enums.ProductChangeKind.UPDATED, last_change_actor=None
    )
    assert product.last_change_old_price is None


def test_product_price_change_has_prices() -> None:
    violates(
        "product_price_change_has_prices",
        ProductFactory.create,
        last_change_at=T0,
        last_change_kind=enums.ProductChangeKind.PRICE_CHANGED,
        last_change_old_price=175_000,
    )
    product = ProductFactory.create(
        last_change_at=T0,
        last_change_kind=enums.ProductChangeKind.PRICE_CHANGED,
        last_change_old_price=175_000,
        last_change_new_price=185_000,
        last_change_actor=StaffUserFactory.create(),
    )
    assert (product.last_change_old_price, product.last_change_new_price) == (175_000, 185_000)


def test_search_text_is_generated_by_the_database() -> None:
    product = ProductFactory.create(
        name_uz="Gialuron Kislotali Serum",
        name_ru="Сыворотка",
        name_en="",
        billz_name="HYALURONIC SERUM 30ML",
        billz_id="4607012301",
    )
    product.refresh_from_db()
    assert product.search_text == "gialuron kislotali serum сыворотка  hyaluronic serum 30ml 4607012301"
    product.billz_name = "RENAMED"
    Product.objects.bulk_update([product], ["billz_name"])  # bypasses save(): the DB maintains the column
    product.refresh_from_db()
    assert product.search_text.endswith(" renamed 4607012301")
    Product.objects.filter(pk=product.pk).update(search_text="x")  # Django omits generated fields silently
    product.refresh_from_db()
    assert product.search_text.endswith(" renamed 4607012301")


def test_category_delete_sets_product_category_null() -> None:
    product = ProductFactory.create()
    product.category.delete()  # type: ignore[union-attr]
    product.refresh_from_db()
    assert product.category is None


# --- ProductStock -------------------------------------------------------------------------------------------


def test_product_stock_is_keyed_by_product_and_cascades() -> None:
    stock = ProductStockFactory.create(quantity=-2)
    assert stock.pk == stock.product_id
    assert str(stock) == f"stock of product {stock.product_id}: -2"
    assert ProductStock.objects.filter(quantity__lte=0).count() == 1
    violates("catalog_productstock_pkey", ProductStock.objects.create, product=stock.product, quantity=1)
    stock.product.delete()
    assert not ProductStock.objects.exists()


# --- ProductImage -------------------------------------------------------------------------------------------


def test_product_image_file_dimensions_and_str() -> None:
    image = ProductImageFactory.create()
    assert str(image) == f"image 0 of product {image.product_id}"
    assert image.file.name == f"products/{image.product_id}/{image.sha256[:16]}.webp"
    assert (image.width, image.height) == (24, 16)
    assert image.file.storage.exists(image.file.name)
    second = ProductImageFactory.create(product=image.product)
    assert second.position == 1
    assert list(image.product.images.all()) == [image, second]


def test_product_image_checks() -> None:
    violates("productimage_position_max_8", ProductImageFactory.create, position=8)
    violates("productimage_content_type", ProductImageFactory.create, content_type="image/gif")
    violates("productimage_content_type", ProductImageFactory.create, content_type="")


def test_product_delete_cascades_images() -> None:
    image = ProductImageFactory.create()
    image.product.delete()
    assert not ProductImage.objects.exists()


@pytest.mark.django_db(transaction=True)
def test_position_unique_is_checked_at_commit_so_a_swap_inside_atomic_succeeds() -> None:
    first = ProductImageFactory.create()
    second = ProductImageFactory.create(product=first.product)
    with transaction.atomic():
        ProductImage.objects.filter(pk=first.pk).update(position=1)
        ProductImage.objects.filter(pk=second.pk).update(position=0)
    assert list(ProductImage.objects.values_list("pk", flat=True)) == [second.pk, first.pk]


@pytest.mark.django_db(transaction=True)
def test_position_unique_fails_a_lone_autocommit_statement() -> None:
    first = ProductImageFactory.create()
    ProductImageFactory.create(product=first.product)
    with pytest.raises(IntegrityError, match="productimage_product_position_unique"):
        ProductImage.objects.filter(pk=first.pk).update(position=1)


@pytest.mark.django_db(transaction=True)
def test_position_unique_duplicate_surfaces_on_exit_from_atomic() -> None:
    first = ProductImageFactory.create()
    second = ProductImageFactory.create(product=first.product)
    with pytest.raises(IntegrityError, match="productimage_product_position_unique"), transaction.atomic():
        ProductImage.objects.filter(pk=second.pk).update(position=0)  # no error here: deferred
        assert ProductImage.objects.filter(position=0).count() == 2
    assert ProductImage.objects.get(pk=second.pk).position == 1  # rolled back at COMMIT


# --- Banner -------------------------------------------------------------------------------------------------


def test_banner_file_dimensions_str_and_ordering() -> None:
    later = BannerFactory.create(title_uz="Ikkinchi", position=1)
    banner = BannerFactory.create(title_uz="Kuzgi chegirmalar", position=0)
    assert str(banner) == f"Kuzgi chegirmalar (#{banner.pk})"
    assert str(banner.image.name).startswith(f"banners/{banner.pk}/") and str(banner.image.name).endswith(
        ".webp"
    )
    assert (banner.image_width, banner.image_height) == (32, 12)
    assert list(Banner.objects.all()) == [banner, later]
    assert banner.link_kind == enums.BannerLinkKind.NONE and banner.starts_at is None


def test_banner_title_and_schedule_checks() -> None:
    violates("banner_title_uz_required", BannerFactory.create, title_uz="")
    violates("banner_ends_after_starts", BannerFactory.create, starts_at=T1, ends_at=T0)
    violates("banner_ends_after_starts", BannerFactory.create, starts_at=T0, ends_at=T0)
    assert BannerFactory.create(starts_at=T0, ends_at=T1).ends_at == T1
    assert BannerFactory.create(starts_at=None, ends_at=T0).starts_at is None


def test_banner_link_kind_targets() -> None:
    product, category = ProductFactory.create(), CategoryFactory.create()
    violates("banner_link_kind_targets", BannerFactory.create, link_kind="none", link_product=product)
    violates("banner_link_kind_targets", BannerFactory.create, link_kind="none", link_category=category)
    violates("banner_link_kind_targets", BannerFactory.create, link_kind="product", link_category=category)
    violates("banner_link_kind_targets", BannerFactory.create, link_kind="category", link_product=product)
    linked = BannerFactory.create(link_kind="product", link_product=product)
    # The target side is lenient so SET_NULL from a deleted target can never violate the check.
    product.delete()
    linked.refresh_from_db()
    assert linked.link_kind == "product" and linked.link_product is None
    by_category = BannerFactory.create(link_kind="category", link_category=category)
    category.delete()
    by_category.refresh_from_db()
    assert by_category.link_category is None


# --- upload_to callables ------------------------------------------------------------------------------------


def test_upload_paths_prefix_the_owner_directory() -> None:
    from glowderma.catalog import media

    category = CategoryFactory.create()
    assert media.category_image_path(category, "dir/abc123.webp") == f"categories/{category.pk}/abc123.webp"
    banner = BannerFactory.create()
    assert media.banner_image_path(banner, "77aa.webp") == f"banners/{banner.pk}/77aa.webp"
    image = ProductImageFactory.create()
    assert media.product_image_path(image, "9f2c.webp") == f"products/{image.product_id}/9f2c.webp"
