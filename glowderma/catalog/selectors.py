"""Read-side helpers of `catalog` (design 01 §6.3–6.5). B3 adds the list/stat selectors on top of these.

``glowderma_ci`` (the ICU case-insensitive collation from core.0007) is used **only** inside the ordering
annotation of :func:`order_products` — never in ``LIKE``, equality, ``DISTINCT``, ``GROUP BY`` or a column's
``db_collation`` (a test walks this module's AST to enforce it). Search goes through the stored, deterministic
``search_text`` column so the trigram GIN index applies; the column and the term get the same search key
(``core.search.SearchKey``: case and ё as decision GC-1, every apostrophe variant removed).
"""

from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import TypedDict

from django.db.models import Count, F, Prefetch, Q, QuerySet, Value
from django.db.models.functions import Coalesce, Collate, NullIf

from glowderma.catalog import seo
from glowderma.catalog.models import Banner, Category, Product, ProductImage, ProductStock
from glowderma.core import enums, timeutils
from glowderma.core.consistency import Finding, finding
from glowderma.core.search import SearchKey, search_key
from glowderma.integrations.billz.ports import BillzProduct

SEARCH_TERM_MAX_LENGTH = 64
MAX_ID_DIGITS = 18  # every 18-digit number fits a bigint id; longer terms can only match the text columns

# Display name = Uzbek name, else the Billz name (what every Uzbek label of a product shows).
DISPLAY_NAME = Coalesce(NullIf(F("name_uz"), Value("")), F("billz_name"))


def filter_products_search(qs: QuerySet[Product], raw: str) -> QuerySet[Product]:
    """Contract §2.3 ``search``: case-insensitive substring over five columns; digits also match ``id``.

    Both sides get the same SQL search key (``core.search.SearchKey``): ``kopik``, ``ko'pik``, ``ko‘pik``,
    ``koʻpik`` and ``ko`pik`` find ``Yumshoq tozalovchi ko‘pik`` (apostrophes removed on both sides), ``елка``
    finds ``ёлка`` (decision GC-1). A term of apostrophes only filters nothing, like a blank one.
    """
    term = raw.strip()[:SEARCH_TERM_MAX_LENGTH]
    if not search_key(term).strip():
        return qs
    # LIKE '%' || key('term') || '%' — the term keyed by PostgreSQL with the very expression of the generated
    # column (a constant the planner pre-evaluates, so the trigram index applies); Django escapes % _ \ in it.
    cond = Q(search_text__contains=SearchKey(Value(term)))
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
# Billz quantities are refreshed by the delta sync every few minutes: a stock row older than a day is stale.
STALE_STOCK_AFTER = timedelta(hours=24)


def stale_stock(stale_before: datetime) -> Q:
    """Products whose Billz quantity was last read before ``stale_before`` (or never), stock row present."""
    return Q(stock__isnull=False) & (Q(stock__synced_at__lt=stale_before) | Q(stock__synced_at__isnull=True))


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


# --- ops console sales & catalog dashboard (glowderma.reporting.dashboards) ---------------------------------


@dataclass(frozen=True, slots=True)
class StockHealth:
    out_of_stock: Finding  # active products with no stock row or a quantity <= 0 (product ids)
    stale: Finding  # active products whose Billz quantity was last read before the cut-off (product ids)
    new_from_billz: int  # created by a Billz import and not reviewed yet (any status)


def stock_health(stale_before: datetime, *, limit: int) -> StockHealth:
    """Stock problems of the sellable catalog, three statements (count + the first ``limit`` ids each)."""
    active = Product.objects.filter(status=enums.ProductStatus.ACTIVE)
    return StockHealth(
        out_of_stock=finding(active.filter(OUT_OF_STOCK), limit=limit),
        stale=finding(active.filter(stale_stock(stale_before)), limit=limit),
        new_from_billz=Product.objects.filter(is_new_from_billz=True).count(),
    )


SEO_TEXT_FIELDS: tuple[str, ...] = tuple(
    f"{name}_{language}" for name in ("name", "seo_title", "seo_description") for language in seo.LANGUAGES
)


@dataclass(slots=True)
class SeoIssueCount:
    kind: str  # "product" | "category"
    code: str
    severity: str
    count: int = 0
    ids: list[int] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class SeoBacklog:
    products_checked: int
    products_with_errors: int
    products_with_warnings: int  # warnings but no error
    categories_checked: int
    categories_with_issues: int  # an error or a warning
    issues: list[SeoIssueCount]  # errors and warnings by (kind, code, severity), most frequent first


def seo_backlog(*, limit: int) -> SeoBacklog:
    """The «SEO holati» checklist (:mod:`glowderma.catalog.seo`) over every active product and active
    category, counted by issue code (a row counts once per code; hints are left out).

    The checklist is Python over loaded rows, so this reads the text columns of the whole active catalog
    (two queries, ``only()`` the checked columns, image and product counts annotated) and runs the checks in
    process: linear in the catalog (a few thousand rows), which is why the dashboard caches it longer than
    its live counts."""
    issues: dict[tuple[str, str, str], SeoIssueCount] = {}

    def add(kind: str, pk: int, found: list[seo.SeoIssue]) -> tuple[bool, bool]:
        seen: set[tuple[str, str]] = set()
        for issue in found:
            if issue.severity == enums.SeoSeverity.INFO or (issue.code, issue.severity) in seen:
                continue
            seen.add((issue.code, issue.severity))
            entry = issues.setdefault(
                (kind, issue.code, issue.severity), SeoIssueCount(kind, issue.code, issue.severity)
            )
            entry.count += 1
            if len(entry.ids) < limit:
                entry.ids.append(pk)
        severities = {severity for _code, severity in seen}
        return enums.SeoSeverity.ERROR in severities, enums.SeoSeverity.WARNING in severities

    products = (
        Product.objects.filter(status=enums.ProductStatus.ACTIVE)
        .only(
            "id",
            "category_id",
            *SEO_TEXT_FIELDS,
            *(f"description_{language}" for language in seo.LANGUAGES),
        )
        .annotate(image_count=Count("images"))
        .order_by("id")
    )
    checked = with_errors = with_warnings = 0
    for product in products:
        checked += 1
        error, warning = add(
            "product", product.pk, seo.product_issues(product, image_count=int(product.image_count))
        )
        with_errors += int(error)
        with_warnings += int(warning and not error)
    categories = (
        Category.objects.filter(is_active=True)
        .only("id", "image", *SEO_TEXT_FIELDS)
        .annotate(active_products=Count("products", filter=Q(products__status=enums.ProductStatus.ACTIVE)))
        .order_by("id")
    )
    categories_checked = categories_with_issues = 0
    for category in categories:
        categories_checked += 1
        error, warning = add(
            "category",
            category.pk,
            seo.category_issues(category, active_products=int(category.active_products)),
        )
        categories_with_issues += int(error or warning)
    ranked = sorted(
        issues.values(),
        key=lambda entry: (
            entry.severity != enums.SeoSeverity.ERROR,
            -entry.count,
            entry.kind,
            entry.code,
        ),
    )
    return SeoBacklog(
        products_checked=checked,
        products_with_errors=with_errors,
        products_with_warnings=with_warnings,
        categories_checked=categories_checked,
        categories_with_issues=categories_with_issues,
        issues=ranked,
    )


def seo_issue_ids(kind: str, code: str, severity: str) -> list[int]:
    """Every row (``kind`` ``product`` or ``category``) the «SEO holati» backlog counts under ``code`` /
    ``severity``: what a backlog row's number covers (its own ``ids`` stop at the dashboard's link limit)."""
    for issue in seo_backlog(limit=10**9).issues:
        if (issue.kind, issue.code, issue.severity) == (kind, code, severity):
            return issue.ids
    return []
