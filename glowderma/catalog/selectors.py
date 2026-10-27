"""Read-side helpers of `catalog` (design 01 §6.3–6.5). B3 adds the list/stat selectors on top of these.

``glowderma_ci`` (the ICU case-insensitive collation from core.0007) is used **only** inside the ordering
annotation of :func:`order_products` — never in ``LIKE``, equality, ``DISTINCT``, ``GROUP BY`` or a column's
``db_collation`` (a test walks this module's AST to enforce it). Search goes through the stored, deterministic
``search_text`` column so the trigram GIN index applies; the column and the term are folded alike
(``core.search.SearchFold``: apostrophe variants and ё, decision GC-1).
"""

from collections.abc import Iterator
from typing import TypedDict

from django.db.models import Count, F, Prefetch, Q, QuerySet, Value
from django.db.models.functions import Coalesce, Collate, NullIf

from glowderma.catalog.models import Banner, Category, Product, ProductImage, ProductStock
from glowderma.core import enums, timeutils
from glowderma.core.search import SearchFold
from glowderma.integrations.billz.ports import BillzProduct

SEARCH_TERM_MAX_LENGTH = 64
MAX_ID_DIGITS = 18  # every 18-digit number fits a bigint id; longer terms can only match the text columns

# Display name = Uzbek name, else the Billz name (what every Uzbek label of a product shows).
DISPLAY_NAME = Coalesce(NullIf(F("name_uz"), Value("")), F("billz_name"))


def filter_products_search(qs: QuerySet[Product], raw: str) -> QuerySet[Product]:
    """Contract §2.3 ``search``: case-insensitive substring over five columns; digits also match ``id``.

    Both sides are folded by the same SQL expression (``core.search.SearchFold``): ``ko'pik``, ``ko‘pik``,
    ``koʻpik`` and ``ko`pik`` find ``Yumshoq tozalovchi ko‘pik``, ``елка`` finds ``ёлка`` (decision GC-1).
    """
    term = raw.strip()[:SEARCH_TERM_MAX_LENGTH]
    if not term:
        return qs
    # LIKE '%' || fold('term') || '%' — the term folded by PostgreSQL with the very expression of the
    # generated column (a constant the planner pre-evaluates, so the trigram index applies); Django escapes
    # % _ \ in it.
    cond = Q(search_text__contains=SearchFold(Value(term)))
    # ASCII digits only: str.isdigit() is also True for "²" or "①", which int() rejects (user input must
    # never raise here). A numeric term also matches the id exactly, never instead of the text.
    if term.isascii() and term.isdigit() and len(term) <= MAX_ID_DIGITS:
        cond |= Q(id=int(term))
    return qs.filter(cond)


def order_products(qs: QuerySet[Product], ordering: str) -> QuerySet[Product]:
    """Contract §2.3 ``ordering``: ``default`` | ``name`` | ``-name`` (design 01 §6.5)."""
    if ordering in ("name", "-name"):
        qs = qs.annotate(_display_ci=Collate(DISPLAY_NAME, "glowderma_ci"))
        return qs.order_by("_display_ci", "id") if ordering == "name" else qs.order_by("-_display_ci", "-id")
    # default: last_change.at desc (falling back to created_at), then id desc — the expression index
    # catalog_product_default_order matches this exactly.
    return qs.annotate(_changed=Coalesce(F("last_change_at"), F("created_at"))).order_by("-_changed", "-id")


# --- B3: admin list / detail / stats selectors --------------------------------------------------------------


def category_queryset() -> QuerySet[Category]:
    """Contract §2.4: ordered by position, ``product_count`` = every product of the category (any status)."""
    return Category.objects.annotate(
        product_count=Count("products", distinct=True),
        # B-W1 «SEO holati»: a category without an active product is an empty page on the website
        active_product_count=Count(
            "products", filter=Q(products__status=enums.ProductStatus.ACTIVE), distinct=True
        ),
    ).order_by("position", "id")


def banner_queryset() -> QuerySet[Banner]:
    return Banner.objects.select_related("link_product", "link_category").order_by("position", "id")


def product_queryset() -> QuerySet[Product]:
    """Products with everything the contract §2.3 shape needs loaded in one pass (no per-row queries)."""
    return (
        Product.objects.select_related("category", "last_change_actor", "stock")
        .prefetch_related(Prefetch("images", queryset=ProductImage.objects.order_by("position", "id")))
        .annotate(stock_quantity=Coalesce(F("stock__quantity"), Value(0)))
    )


def get_product(product_id: int) -> Product | None:
    return product_queryset().filter(pk=product_id).first()


class ProductFilters(TypedDict, total=False):
    search: str
    category: Category | None  # ``None`` = "no category"; absent = any
    status: str
    promo: bool
    out_of_stock: bool
    is_new: bool
    ordering: str


OUT_OF_STOCK = Q(stock__quantity__lte=0) | Q(stock__isnull=True)


def filter_products(qs: QuerySet[Product], filters: ProductFilters) -> QuerySet[Product]:
    """Contract §2.3 ``GET products/`` filters and ordering over an already annotated queryset."""
    if "search" in filters:
        qs = filter_products_search(qs, filters["search"])
    if "category" in filters:
        category = filters["category"]
        qs = qs.filter(category__isnull=True) if category is None else qs.filter(category=category)
    if "status" in filters:
        qs = qs.filter(status=filters["status"])
    if "promo" in filters:
        qs = qs.filter(promo=filters["promo"])
    if "out_of_stock" in filters:
        qs = qs.filter(OUT_OF_STOCK) if filters["out_of_stock"] else qs.exclude(OUT_OF_STOCK)
    if "is_new" in filters:
        qs = qs.filter(is_new_from_billz=filters["is_new"])
    return order_products(qs, filters.get("ordering", "default"))


def product_counts() -> dict[str, int]:
    """Contract §2.3 ``products/stats/`` counters (``billz`` is added by the view)."""
    counts = Product.objects.aggregate(
        total=Count("id"),
        active=Count("id", filter=Q(status=enums.ProductStatus.ACTIVE)),
        hidden=Count("id", filter=Q(status=enums.ProductStatus.HIDDEN)),
        new_from_billz=Count("id", filter=Q(is_new_from_billz=True)),
        out_of_stock=Count("id", filter=OUT_OF_STOCK),
    )
    return {key: int(value or 0) for key, value in counts.items()}


def stock_of(product: Product) -> ProductStock | None:
    """The product's stock row (``select_related("stock")``), ``None`` when Billz never wrote one."""
    try:
        return product.stock
    except ProductStock.DoesNotExist:
        return None


def billz_view(product: Product) -> BillzProduct:
    """The Billz-owned fields of a product as a port row (what Billz "currently says" about it)."""
    stock = stock_of(product)
    return BillzProduct(
        billz_id=product.billz_id,
        billz_name=product.billz_name,
        volume=product.volume,
        price=product.billz_price,
        stock=stock.quantity if stock is not None else 0,
        synced_at=(stock.synced_at if stock is not None else None)
        or product.billz_synced_at
        or timeutils.now(),
    )


class CatalogKnownProducts:
    """``integrations.billz.ports.KnownProducts`` over the catalog tables (handed to the fake adapter)."""

    def get(self, billz_id: str) -> BillzProduct | None:
        product = Product.objects.select_related("stock").filter(billz_id=billz_id).first()
        return None if product is None else billz_view(product)

    def all(self) -> Iterator[BillzProduct]:
        for product in Product.objects.select_related("stock").order_by("id").iterator(chunk_size=200):
            yield billz_view(product)


# --- M2: customer catalog visibility (customer contract §2.3; P0, P1 appends its helpers below) ---

# A product is sold online only at or above this price (customer contract §2.3).
CUSTOMER_MIN_PRICE = 1000


def customer_visible_products() -> QuerySet[Product]:
    """Products a customer may see and buy: ``active``, ``price ≥ 1000``, in an active category.

    A hidden category hides its products (design 01 open point 12). Anything else is 404 in the catalog and
    ``unavailable`` in the cart; checkout re-checks it under the stock locks.
    """
    return Product.objects.filter(
        status=enums.ProductStatus.ACTIVE,
        price__gte=CUSTOMER_MIN_PRICE,
        category__isnull=False,
        category__is_active=True,
    )
