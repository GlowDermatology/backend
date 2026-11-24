"""The customer catalog's products (customer contract §2.3): the visible rows, the orderings, the cards and
the detail."""

from __future__ import annotations

from typing import Any

from django.conf import settings
from django.db.models import F, QuerySet, Value
from django.db.models.functions import Coalesce, Collate, NullIf

from glowderma.catalog import selectors as catalog_selectors
from glowderma.catalog.models import Product, ProductImage
from glowderma.commerce import selectors as commerce_selectors
from glowderma.commerce.models import CartItem
from glowderma.core import enums, i18n
from glowderma.customers.selectors import common


def visible_product_ids(ids: list[int]) -> set[int]:
    return set(catalog_selectors.customer_visible_products().filter(pk__in=ids).values_list("pk", flat=True))


def customer_products() -> QuerySet[Product]:
    return catalog_selectors.customer_visible_products().select_related("category")


def resolved_name_expression(lang: str) -> Coalesce:
    columns = [f"name_{lang}"] if lang != enums.ContentLanguage.UZ else []
    parts = [NullIf(F(column), Value("")) for column in (*columns, "name_uz")]
    return Coalesce(*parts, F("billz_name"))


def order_customer_products(qs: QuerySet[Product], ordering: str, lang: str) -> QuerySet[Product]:
    """``default`` (newest ``created_at``), ``price``, ``-price``, ``name`` (resolved language under
    ``glowderma_ci``); ties by ``id`` desc."""
    if ordering == enums.CustomerProductOrdering.PRICE:
        return qs.order_by("price", "-id")
    if ordering == enums.CustomerProductOrdering.PRICE_DESC:
        return qs.order_by("-price", "-id")
    if ordering == enums.CustomerProductOrdering.NAME:
        return qs.annotate(_name_ci=Collate(resolved_name_expression(lang), common.NAME_COLLATION)).order_by(
            "_name_ci", "-id"
        )
    return qs.order_by("-created_at", "-id")


def filter_in_stock(qs: QuerySet[Product], in_stock: bool) -> QuerySet[Product]:
    """``available > 0`` (``customer_available_stock``: nothing while online selling is off)."""
    ids = list(qs.values_list("pk", flat=True))
    available = commerce_selectors.customer_available_stock(ids)
    positive = [pk for pk, quantity in available.items() if quantity > 0]
    return qs.filter(pk__in=positive) if in_stock else qs.exclude(pk__in=positive)


def _main_images(product_ids: list[int]) -> dict[int, ProductImage]:
    images: dict[int, ProductImage] = {}
    for image in ProductImage.objects.filter(product_id__in=product_ids).order_by(
        "product_id", "position", "id"
    ):
        images.setdefault(image.product_id, image)
    return images


def _cart_quantities(customer_id: int | None, product_ids: list[int]) -> dict[int, int]:
    if customer_id is None:
        return {}
    return dict(
        CartItem.objects.filter(cart__customer_id=customer_id, product_id__in=product_ids).values_list(
            "product_id", "quantity"
        )
    )


def product_cards(products: list[Product], lang: str, customer_id: int | None = None) -> list[dict[str, Any]]:
    """``ProductCard`` rows: one stock read (``customer_available_stock``), one image query, one cart
    query."""
    ids = [product.pk for product in products]
    available = commerce_selectors.customer_available_stock(ids)
    images = _main_images(ids)
    in_cart = _cart_quantities(customer_id, ids)
    cards = []
    for product in products:
        quantity = int(available.get(product.pk, 0))
        image = images.get(product.pk)
        category = product.category
        cards.append(
            {
                "id": product.pk,
                "slug": product.slug,
                "name": common.product_name(product, lang),
                "content_language": i18n.content_language(product, "name", lang, fallback_text=True),
                "sku": product.billz_id or None,  # the Billz id / barcode: the only article code we hold
                "volume": product.volume,
                "price": product.price,
                "promo": product.promo,
                "category": {"slug": category.slug, "name": common.localized_field(category, "name", lang)}
                if category is not None
                else None,
                **common.media_urls("main_image", image.file if image is not None else None),
                "available": quantity,
                "stock_status": (
                    enums.StockStatus.IN_STOCK if quantity > 0 else enums.StockStatus.OUT_OF_STOCK
                ).value,
                "in_cart_quantity": int(in_cart.get(product.pk, 0)),
                "updated_at": product.updated_at,
            }
        )
    return cards


def product_detail(product: Product, lang: str, customer_id: int | None = None) -> dict[str, Any]:
    [card] = product_cards([product], lang, customer_id)
    images = ProductImage.objects.filter(product_id=product.pk).order_by("position", "id")
    return {
        **card,
        "short_description": common.localized_field(product, "short_description", lang),
        "description": common.localized_field(product, "description", lang),
        "ingredients": common.localized_field(product, "ingredients", lang),
        "usage": common.localized_field(product, "usage", lang),
        "images": [
            {
                "id": image.pk,
                **common.media_urls("", image.file),
                "position": image.position,
                # the image's own alt text (this language, else Uzbek), else the product name
                "alt": common.localized_field(image, "alt", lang) or card["name"],
            }
            for image in images
        ],
        "max_quantity": min(card["available"], int(settings.CART_MAX_QTY_PER_LINE)),
        **common.seo_payload(product, lang),
    }


__all__ = [
    "customer_products",
    "filter_in_stock",
    "order_customer_products",
    "product_cards",
    "product_detail",
    "resolved_name_expression",
    "visible_product_ids",
]
