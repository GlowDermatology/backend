"""Models of `catalog` — docs/plans/m1-models/01-… §4 (slice B2).

Billz owns ``billz_*``, ``volume`` and stock; Glow Derma owns texts, images, visibility, the promo flag and
the price. ``Product.search_text`` is a stored generated column maintained by PostgreSQL (§6.2), so no code
path (import ``bulk_update``, seeder, raw SQL) can let it drift.
"""

from django.contrib.postgres.indexes import GinIndex
from django.db import models
from django.db.models import Deferrable, F, Func, Q, TextField, Value
from django.db.models.functions import Coalesce

from glowderma.catalog import media
from glowderma.core import enums
from glowderma.core.models import TimeStampedModel
from glowderma.core.search import SearchFold


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
    name_uz = models.CharField(max_length=60)
    name_ru = models.CharField(max_length=60, blank=True)
    name_en = models.CharField(max_length=60, blank=True)
    position = models.PositiveIntegerField(default=0)
    is_active = models.BooleanField(default=True)
    image = models.ImageField(upload_to=media.category_image_path, blank=True, max_length=255)

    class Meta:
        verbose_name_plural = "categories"
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
    billz_id = models.CharField(max_length=14, unique=True)  # ^\d{6,14}$ (Billz product id or barcode)
    billz_name = models.CharField(max_length=255, blank=True)
    volume = models.CharField(max_length=40, blank=True)  # "30 ml"
    billz_price = models.BigIntegerField(null=True)  # reference only (O6)
    billz_synced_at = models.DateTimeField(null=True)  # last time billz_* were refreshed
    # Glow Derma-owned
    price = models.BigIntegerField(default=0)  # admin price; the API requires 1000..100_000_000
    category = models.ForeignKey(
        "catalog.Category", null=True, on_delete=models.SET_NULL, related_name="products"
    )
    promo = models.BooleanField(default=False)
    status = models.CharField(
        max_length=8, choices=enums.ProductStatus.choices, default=enums.ProductStatus.HIDDEN, db_index=True
    )
    is_new_from_billz = models.BooleanField(default=False)
    name_uz = models.CharField(max_length=120, blank=True)
    name_ru = models.CharField(max_length=120, blank=True)
    name_en = models.CharField(max_length=120, blank=True)
    description_uz = models.TextField(blank=True)  # ≤2000 (serializer)
    description_ru = models.TextField(blank=True)
    description_en = models.TextField(blank=True)
    ingredients_uz = models.TextField(blank=True)  # ≤1000
    ingredients_ru = models.TextField(blank=True)
    ingredients_en = models.TextField(blank=True)
    usage_uz = models.TextField(blank=True)  # ≤1000
    usage_ru = models.TextField(blank=True)
    usage_en = models.TextField(blank=True)
    # search (stored generated column, §6.2): folded like every search term (core.search, decision GC-1), so
    # ko'pik / ko‘pik / koʻpik and ё / е match each other
    search_text = models.GeneratedField(
        expression=SearchFold(
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
    last_change_at = models.DateTimeField(null=True)
    last_change_kind = models.CharField(max_length=16, choices=enums.ProductChangeKind.choices, blank=True)
    last_change_old_price = models.BigIntegerField(null=True)
    last_change_new_price = models.BigIntegerField(null=True)
    last_change_actor = models.ForeignKey(
        "accounts.StaffUser", null=True, on_delete=models.SET_NULL, related_name="+"
    )

    class Meta:
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


class ProductStock(models.Model):
    """Billz-owned stock quantity, one row per product, written only by ``catalog.services.sync_stock``."""

    product = models.OneToOneField(
        "catalog.Product", primary_key=True, on_delete=models.CASCADE, related_name="stock"
    )
    quantity = models.IntegerField(default=0)  # Billz can report negatives after returns
    synced_at = models.DateTimeField(null=True)  # when quantity was last read from Billz
    billz_updated_at = models.DateTimeField(null=True)  # Billz's own timestamp when provided

    class Meta:
        indexes = [models.Index(fields=["quantity"], condition=Q(quantity__lte=0), name="catalog_stock_out")]

    def __str__(self) -> str:
        return f"stock of product {self.product_id}: {self.quantity}"


class ProductImage(TimeStampedModel):
    """An uploaded, re-encoded photo of a product with an explicit gallery position."""

    product = models.ForeignKey("catalog.Product", on_delete=models.CASCADE, related_name="images")
    file = models.ImageField(
        upload_to=media.product_image_path, max_length=255, width_field="width", height_field="height"
    )
    position = models.PositiveSmallIntegerField()
    width = models.PositiveIntegerField()
    height = models.PositiveIntegerField()
    content_type = models.CharField(max_length=32)  # image/jpeg | image/png | image/webp (after re-encode)
    size_bytes = models.PositiveIntegerField()
    sha256 = models.CharField(max_length=64)  # of the stored (re-encoded) bytes; also the file stem
    uploaded_by = models.ForeignKey(
        "accounts.StaffUser", null=True, on_delete=models.SET_NULL, related_name="+"
    )

    class Meta:
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

    title_uz = models.CharField(max_length=80)
    title_ru = models.CharField(max_length=80, blank=True)
    title_en = models.CharField(max_length=80, blank=True)
    image = models.ImageField(
        upload_to=media.banner_image_path,
        max_length=255,
        width_field="image_width",
        height_field="image_height",
    )
    image_width = models.PositiveIntegerField()
    image_height = models.PositiveIntegerField()
    link_kind = models.CharField(
        max_length=8, choices=enums.BannerLinkKind.choices, default=enums.BannerLinkKind.NONE
    )
    link_product = models.ForeignKey(
        "catalog.Product", null=True, on_delete=models.SET_NULL, related_name="banners"
    )
    link_category = models.ForeignKey(
        "catalog.Category", null=True, on_delete=models.SET_NULL, related_name="banners"
    )
    is_active = models.BooleanField(default=True)
    starts_at = models.DateTimeField(null=True)
    ends_at = models.DateTimeField(null=True)
    position = models.PositiveIntegerField(default=0)

    class Meta:
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
                    )
                    | Q(link_kind=enums.BannerLinkKind.PRODUCT, link_category__isnull=True)
                    | Q(link_kind=enums.BannerLinkKind.CATEGORY, link_product__isnull=True)
                ),
                name="banner_link_kind_targets",
            ),
        ]
        indexes = [models.Index(fields=["position", "id"], name="catalog_banner_position")]
        ordering = ["position", "id"]

    def __str__(self) -> str:
        return f"{self.title_uz} (#{self.pk})"
