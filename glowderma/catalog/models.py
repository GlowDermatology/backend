"""Models of `catalog` — docs/plans/m1-models/01-… §4 (slice B2).

Billz owns ``billz_*``, ``volume`` and stock; Glow Derma owns texts, images, visibility, the promo flag and
the price. ``Product.search_text`` is a stored generated column maintained by PostgreSQL (§6.2), so no code
path (import ``bulk_update``, seeder, raw SQL) can let it drift.
"""

from typing import Any

from django.contrib.postgres.indexes import GinIndex
from django.db import models
from django.db.models import Deferrable, F, Func, Q, TextField, Value
from django.db.models.functions import Coalesce

from glowderma.catalog import media
from glowderma.core import enums, slugs
from glowderma.core.models import TimeStampedModel
from glowderma.core.search import SearchKey

SLUG_MAX_LENGTH = 140
PRODUCT_SLUG_CONSTRAINT = "product_slug_unique"


def product_slug_base(name_uz: str, billz_name: str, billz_id: str) -> str:
    """The derived slug of a product (``Product.save`` and the ``catalog.0005`` backfill)."""
    return slugs.derive_slug(
        name_uz, billz_name, prefix="mahsulot", fallback=billz_id, max_length=SLUG_MAX_LENGTH
    )


class JoinText(Func):
    """``a || b || c …`` — immutable text concatenation usable in a stored generated column.

    PostgreSQL's ``CONCAT()`` (what Django's ``Concat`` emits) is only STABLE and is refused in a generated
    column; ``textcat`` (``||``) and ``lower(text)`` are IMMUTABLE.
    """

    arg_joiner = " || "
    template = "(%(expressions)s)"
    output_field = TextField()


class Category(TimeStampedModel):
    """A product grouping addressed by its immutable slug."""

    slug = models.SlugField(max_length=40, unique=True)  # ^[a-z0-9-]{2,40}$, never "order"
    name_uz = models.CharField(max_length=60, verbose_name="nomi (uz)")
    name_ru = models.CharField(max_length=60, blank=True, verbose_name="nomi (ru)")
    name_en = models.CharField(max_length=60, blank=True, verbose_name="nomi (en)")
    position = models.PositiveIntegerField(default=0, verbose_name="tartib")
    is_active = models.BooleanField(default=True, verbose_name="faol")
    image = models.ImageField(
        upload_to=media.category_image_path, blank=True, max_length=255, verbose_name="rasm"
    )
    # B-W1 SEO (admin contract §2.4): optional per-language page title and meta description; "" = the
    # website builds one from the name.
    seo_title_uz = models.CharField(
        max_length=120, blank=True, default="", db_default="", verbose_name="SEO sarlavha (uz)"
    )
    seo_title_ru = models.CharField(
        max_length=120, blank=True, default="", db_default="", verbose_name="SEO sarlavha (ru)"
    )
    seo_title_en = models.CharField(
        max_length=120, blank=True, default="", db_default="", verbose_name="SEO sarlavha (en)"
    )
    seo_description_uz = models.CharField(
        max_length=300, blank=True, default="", db_default="", verbose_name="SEO tavsif (uz)"
    )
    seo_description_ru = models.CharField(
        max_length=300, blank=True, default="", db_default="", verbose_name="SEO tavsif (ru)"
    )
    seo_description_en = models.CharField(
        max_length=300, blank=True, default="", db_default="", verbose_name="SEO tavsif (en)"
    )

    class Meta:
        verbose_name = "kategoriya"
        verbose_name_plural = "kategoriyalar"
        constraints = [
            models.CheckConstraint(
                condition=Q(slug__regex=r"^[a-z0-9-]{2,40}$"), name="category_slug_format"
            ),
            models.CheckConstraint(condition=~Q(slug="order"), name="category_slug_not_reserved"),
            models.CheckConstraint(condition=~Q(name_uz=""), name="category_name_uz_required"),
        ]
        indexes = [models.Index(fields=["position", "id"], name="catalog_category_position")]
        ordering = ["position", "id"]

    def __str__(self) -> str:
        return f"{self.slug}: {self.name_uz}"


class Product(TimeStampedModel):
    """A sellable item."""

    # Billz-owned (read-only in the admin API)
    # ^\d{6,14}$ (Billz product id or barcode)
    billz_id = models.CharField(max_length=14, unique=True, verbose_name="Billz ID")
    billz_name = models.CharField(max_length=255, blank=True, verbose_name="Billz nomi")
    volume = models.CharField(max_length=40, blank=True, verbose_name="hajm")  # "30 ml"
    billz_price = models.BigIntegerField(null=True, verbose_name="Billz narxi")  # reference only (O6)
    # last time billz_* were refreshed
    billz_synced_at = models.DateTimeField(null=True, verbose_name="Billz bilan sinxronlangan")
    # Glow Derma-owned
    # admin price; the API requires 1000..100_000_000
    price = models.BigIntegerField(default=0, verbose_name="narx")
    category = models.ForeignKey(
        "catalog.Category",
        null=True,
        on_delete=models.SET_NULL,
        related_name="products",
        verbose_name="kategoriya",
    )
    promo = models.BooleanField(default=False, verbose_name="aksiya")
    status = models.CharField(
        max_length=8,
        choices=enums.ProductStatus.choices,
        default=enums.ProductStatus.HIDDEN,
        db_index=True,
        verbose_name="holat",
    )
    is_new_from_billz = models.BooleanField(default=False, verbose_name="Billz’dan yangi")
    name_uz = models.CharField(max_length=120, blank=True, verbose_name="nomi (uz)")
    name_ru = models.CharField(max_length=120, blank=True, verbose_name="nomi (ru)")
    name_en = models.CharField(max_length=120, blank=True, verbose_name="nomi (en)")
    description_uz = models.TextField(blank=True, verbose_name="tavsif (uz)")  # ≤2000 (serializer)
    description_ru = models.TextField(blank=True, verbose_name="tavsif (ru)")
    description_en = models.TextField(blank=True, verbose_name="tavsif (en)")
    ingredients_uz = models.TextField(blank=True, verbose_name="tarkib (uz)")  # ≤1000
    ingredients_ru = models.TextField(blank=True, verbose_name="tarkib (ru)")
    ingredients_en = models.TextField(blank=True, verbose_name="tarkib (en)")
    usage_uz = models.TextField(blank=True, verbose_name="qo‘llash (uz)")  # ≤1000
    usage_ru = models.TextField(blank=True, verbose_name="qo‘llash (ru)")
    usage_en = models.TextField(blank=True, verbose_name="qo‘llash (en)")
    # B-W1 SEO (admin contract §2.3): the website's URL segment — set once from the Uzbek name (else the Billz
    # name) when the row is created, unique, never only digits, edited only by staff (stable URLs).
    slug = models.CharField(max_length=SLUG_MAX_LENGTH, blank=True, default="", db_default="")
    seo_title_uz = models.CharField(
        max_length=120, blank=True, default="", db_default="", verbose_name="SEO sarlavha (uz)"
    )
    seo_title_ru = models.CharField(
        max_length=120, blank=True, default="", db_default="", verbose_name="SEO sarlavha (ru)"
    )
    seo_title_en = models.CharField(
        max_length=120, blank=True, default="", db_default="", verbose_name="SEO sarlavha (en)"
    )
    seo_description_uz = models.CharField(
        max_length=300, blank=True, default="", db_default="", verbose_name="SEO tavsif (uz)"
    )
    seo_description_ru = models.CharField(
        max_length=300, blank=True, default="", db_default="", verbose_name="SEO tavsif (ru)"
    )
    seo_description_en = models.CharField(
        max_length=300, blank=True, default="", db_default="", verbose_name="SEO tavsif (en)"
    )
    # search (stored generated column, §6.2): the search key of every search term (core.search.SearchKey:
    # case, ё = е, decision GC-1; apostrophes removed, catalog.0006), so kopik / ko'pik / ko‘pik / koʻpik and
    # ё / е match each other
    search_text = models.GeneratedField(
        expression=SearchKey(
            JoinText(
                F("name_uz"),
                Value(" "),
                F("name_ru"),
                Value(" "),
                F("name_en"),
                Value(" "),
                F("billz_name"),
                Value(" "),
                F("billz_id"),
            )
        ),
        output_field=models.TextField(),
        db_persist=True,
    )
    # last admin change (contract last_change; null when never edited through the API)
    last_change_at = models.DateTimeField(null=True, verbose_name="oxirgi o‘zgarish")
    last_change_kind = models.CharField(max_length=16, choices=enums.ProductChangeKind.choices, blank=True)
    last_change_old_price = models.BigIntegerField(null=True)
    last_change_new_price = models.BigIntegerField(null=True)
    last_change_actor = models.ForeignKey(
        "accounts.StaffUser", null=True, on_delete=models.SET_NULL, related_name="+"
    )

    class Meta:
        verbose_name = "mahsulot"
        verbose_name_plural = "mahsulotlar"
        constraints = [
            models.CheckConstraint(
                condition=Q(billz_id__regex=r"^\d{6,14}$"), name="product_billz_id_format"
            ),
            models.CheckConstraint(condition=Q(price__gte=0), name="product_price_non_negative"),
            models.CheckConstraint(
                condition=Q(billz_price__isnull=True) | Q(billz_price__gte=0),
                name="product_billz_price_non_negative",
            ),
            models.CheckConstraint(
                condition=(
                    Q(
                        last_change_at__isnull=True,
                        last_change_kind="",
                        last_change_actor__isnull=True,
                        last_change_old_price__isnull=True,
                        last_change_new_price__isnull=True,
                    )
                    | (Q(last_change_at__isnull=False) & ~Q(last_change_kind=""))
                ),
                name="product_last_change_all_or_nothing",
            ),
            models.CheckConstraint(
                condition=~Q(last_change_kind=enums.ProductChangeKind.PRICE_CHANGED)
                | Q(last_change_old_price__isnull=False, last_change_new_price__isnull=False),
                name="product_price_change_has_prices",
            ),
            # "" = a row written by the previous release during a deploy; the next save derives one
            models.UniqueConstraint(fields=["slug"], condition=~Q(slug=""), name=PRODUCT_SLUG_CONSTRAINT),
            models.CheckConstraint(
                condition=Q(slug="")
                | (Q(slug__regex=slugs.SLUG_PATTERN) & ~Q(slug__regex=slugs.DIGITS_ONLY_PATTERN)),
                name="product_slug_format",
            ),
        ]
        indexes = [
            GinIndex(fields=["search_text"], name="catalog_product_search_trgm", opclasses=["gin_trgm_ops"]),
            models.Index(fields=["category", "status"], name="catalog_product_cat_status"),
            models.Index(
                Coalesce("last_change_at", "created_at").desc(),
                F("id").desc(),
                name="catalog_product_default_order",
            ),  # exact match for ordering=default
            models.Index(
                fields=["is_new_from_billz"], condition=Q(is_new_from_billz=True), name="catalog_product_new"
            ),
        ]
        ordering = ["-id"]

    def __str__(self) -> str:
        return self.name_uz or self.billz_name or self.billz_id

    @property
    def display_name(self) -> str:
        return self.name_uz or self.billz_name

    def slug_base(self) -> str:
        return product_slug_base(self.name_uz, self.billz_name, self.billz_id)

    def save(self, *args: Any, **kwargs: Any) -> None:
        """A new row without a slug gets the first free one derived from its name (B-W1)."""
        if self.slug:
            super().save(*args, **kwargs)
            return
        if kwargs.get("update_fields") is not None:
            kwargs["update_fields"] = {*kwargs["update_fields"], "slug"}  # the derived slug is written too
        slugs.save_with_unique_slug(
            self,
            lambda: super(Product, self).save(*args, **kwargs),
            base=self.slug_base(),
            constraint=PRODUCT_SLUG_CONSTRAINT,
            max_length=SLUG_MAX_LENGTH,
        )


class ProductStock(models.Model):
    """Billz-owned stock quantity, one row per product, written only by ``catalog.services.sync_stock``."""

    product = models.OneToOneField(
        "catalog.Product",
        primary_key=True,
        on_delete=models.CASCADE,
        related_name="stock",
        verbose_name="mahsulot",
    )
    # Billz can report negatives after returns
    quantity = models.IntegerField(default=0, verbose_name="miqdor")
    # when quantity was last read from Billz
    synced_at = models.DateTimeField(null=True, verbose_name="sinxronlangan")
    billz_updated_at = models.DateTimeField(null=True)  # Billz's own timestamp when provided

    class Meta:
        verbose_name = "qoldiq"
        verbose_name_plural = "qoldiqlar"
        indexes = [models.Index(fields=["quantity"], condition=Q(quantity__lte=0), name="catalog_stock_out")]

    def __str__(self) -> str:
        return f"stock of product {self.product_id}: {self.quantity}"


class ProductImage(TimeStampedModel):
    """An uploaded, re-encoded photo of a product with an explicit gallery position."""

    product = models.ForeignKey(
        "catalog.Product", on_delete=models.CASCADE, related_name="images", verbose_name="mahsulot"
    )
    file = models.ImageField(
        upload_to=media.product_image_path,
        max_length=255,
        width_field="width",
        height_field="height",
        verbose_name="fayl",
    )
    position = models.PositiveSmallIntegerField(verbose_name="tartib")
    width = models.PositiveIntegerField(verbose_name="eni")
    height = models.PositiveIntegerField(verbose_name="bo‘yi")
    # image/jpeg | image/png | image/webp (after re-encode)
    content_type = models.CharField(max_length=32, verbose_name="fayl turi")
    size_bytes = models.PositiveIntegerField(verbose_name="hajmi (bayt)")
    sha256 = models.CharField(max_length=64)  # of the stored (re-encoded) bytes; also the file stem
    uploaded_by = models.ForeignKey(
        "accounts.StaffUser", null=True, on_delete=models.SET_NULL, related_name="+", verbose_name="yuklagan"
    )

    class Meta:
        verbose_name = "mahsulot rasmi"
        verbose_name_plural = "mahsulot rasmlari"
        constraints = [
            models.UniqueConstraint(
                fields=["product", "position"],
                name="productimage_product_position_unique",
                deferrable=Deferrable.DEFERRED,
            ),
            models.CheckConstraint(condition=Q(position__lte=7), name="productimage_position_max_8"),
            models.CheckConstraint(
                condition=Q(content_type__in=["image/jpeg", "image/png", "image/webp"]),
                name="productimage_content_type",
            ),
        ]
        ordering = ["position", "id"]

    def __str__(self) -> str:
        return f"image {self.position} of product {self.product_id}"


class Banner(TimeStampedModel):
    """A home-screen banner with an optional deep link and an optional schedule."""

    title_uz = models.CharField(max_length=80, verbose_name="sarlavha (uz)")
    title_ru = models.CharField(max_length=80, blank=True, verbose_name="sarlavha (ru)")
    title_en = models.CharField(max_length=80, blank=True, verbose_name="sarlavha (en)")
    image = models.ImageField(
        upload_to=media.banner_image_path,
        max_length=255,
        width_field="image_width",
        height_field="image_height",
        verbose_name="rasm",
    )
    image_width = models.PositiveIntegerField()
    image_height = models.PositiveIntegerField()
    link_kind = models.CharField(
        max_length=8,
        choices=enums.BannerLinkKind.choices,
        default=enums.BannerLinkKind.NONE,
        verbose_name="havola turi",
    )
    link_product = models.ForeignKey(
        "catalog.Product",
        null=True,
        on_delete=models.SET_NULL,
        related_name="banners",
        verbose_name="havola mahsuloti",
    )
    link_category = models.ForeignKey(
        "catalog.Category",
        null=True,
        on_delete=models.SET_NULL,
        related_name="banners",
        verbose_name="havola kategoriyasi",
    )
    # gap X-BANNER-URL: an https link the app opens (kind "url" only; "" otherwise; decision GC-3)
    link_url = models.CharField(max_length=500, blank=True, default="", verbose_name="havola URL")
    is_active = models.BooleanField(default=True, verbose_name="faol")
    starts_at = models.DateTimeField(null=True, verbose_name="boshlanishi")
    ends_at = models.DateTimeField(null=True, verbose_name="tugashi")
    position = models.PositiveIntegerField(default=0, verbose_name="tartib")

    class Meta:
        verbose_name = "banner"
        verbose_name_plural = "bannerlar"
        constraints = [
            models.CheckConstraint(condition=~Q(title_uz=""), name="banner_title_uz_required"),
            models.CheckConstraint(
                condition=Q(starts_at__isnull=True) | Q(ends_at__isnull=True) | Q(ends_at__gt=F("starts_at")),
                name="banner_ends_after_starts",
            ),
            models.CheckConstraint(
                condition=(
                    Q(
                        link_kind=enums.BannerLinkKind.NONE,
                        link_product__isnull=True,
                        link_category__isnull=True,
                        link_url="",
                    )
                    | Q(link_kind=enums.BannerLinkKind.PRODUCT, link_category__isnull=True, link_url="")
                    | Q(link_kind=enums.BannerLinkKind.CATEGORY, link_product__isnull=True, link_url="")
                    | Q(
                        link_kind=enums.BannerLinkKind.URL,
                        link_product__isnull=True,
                        link_category__isnull=True,
                        link_url__startswith="https://",
                    )
                ),
                name="banner_link_kind_targets",
            ),
        ]
        indexes = [models.Index(fields=["position", "id"], name="catalog_banner_position")]
        ordering = ["position", "id"]

    def __str__(self) -> str:
        return f"{self.title_uz} (#{self.pk})"
