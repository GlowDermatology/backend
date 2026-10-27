"""Read-only Django admin for the catalog (design 01 §11). Writes go through the admin API only."""

from glowderma.catalog.models import Banner, Category, Product, ProductImage, ProductStock
from glowderma.core.admin import register_read_only

register_read_only(
    Category,
    list_display=("slug", "name_uz", "position", "is_active"),
    list_filter=("is_active",),
    search_fields=("slug", "name_uz"),
    ordering=("position", "id"),
)
register_read_only(
    Product,
    list_display=(
        "id",
        "billz_id",
        "name_uz",
        "billz_name",
        "category",
        "price",
        "status",
        "promo",
        "is_new_from_billz",
    ),
    list_filter=("status", "promo", "is_new_from_billz", "category"),
    search_fields=("billz_id", "name_uz", "billz_name"),
    ordering=("-id",),
    raw_id_fields=("category", "last_change_actor"),
)
register_read_only(
    ProductStock,
    list_display=("product", "quantity", "synced_at"),
    search_fields=("product__billz_id",),
    ordering=("product_id",),
    raw_id_fields=("product",),
)
register_read_only(
    ProductImage,
    list_display=("product", "position", "content_type", "width", "height", "uploaded_by"),
    search_fields=("product__billz_id",),
    ordering=("product_id", "position"),
    raw_id_fields=("product", "uploaded_by"),
)
register_read_only(
    Banner,
    list_display=("id", "title_uz", "link_kind", "is_active", "starts_at", "ends_at", "position"),
    list_filter=("link_kind", "is_active"),
    search_fields=("title_uz",),
    ordering=("position", "id"),
    raw_id_fields=("link_product", "link_category"),
)
