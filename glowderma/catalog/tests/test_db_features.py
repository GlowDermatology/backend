"""Design 01 §6: generated column, trigram GIN index, search/ordering semantics and the collation rule."""

import ast
import inspect
from datetime import datetime

import pytest
from django.db import NotSupportedError, ProgrammingError, connection, transaction

from glowderma.catalog import selectors
from glowderma.catalog.models import Product
from glowderma.catalog.selectors import filter_products_search, order_products
from glowderma.catalog.tests.factories import ProductFactory
from glowderma.core import enums
from glowderma.core.timeutils import TASHKENT

pytestmark = pytest.mark.django_db


def fetch_one(sql: str, params: list[str] | None = None) -> tuple[object, ...] | None:
    with connection.cursor() as cursor:
        cursor.execute(sql, params)
        row = cursor.fetchone()
        return tuple(row) if row is not None else None


def index_definitions(table: str) -> dict[str, str]:
    with connection.cursor() as cursor:
        cursor.execute("SELECT indexname, indexdef FROM pg_indexes WHERE tablename = %s", [table])
        return {name: definition for name, definition in cursor.fetchall()}


def test_search_text_is_a_stored_generated_column() -> None:
    row = fetch_one(
        "SELECT attgenerated, pg_get_expr(d.adbin, d.adrelid) FROM pg_attribute a "
        "JOIN pg_attrdef d ON d.adrelid = a.attrelid AND d.adnum = a.attnum "
        "WHERE a.attrelid = 'catalog_product'::regclass AND a.attname = 'search_text'"
    )
    assert row is not None and row[0] == "s"
    expression = str(row[1]).lower()
    assert "lower(" in expression and "||" in expression and "concat" not in expression
    # folded like every search term (core.search, decision GC-1): apostrophe variants and ё
    assert expression.startswith("translate(lower(")
    for column in ("name_uz", "name_ru", "name_en", "billz_name", "billz_id"):
        assert column in expression


def test_product_indexes_from_pg_catalog() -> None:
    definitions = index_definitions("catalog_product")
    trgm = definitions["catalog_product_search_trgm"]
    assert "USING gin (search_text gin_trgm_ops)" in trgm
    default_order = definitions["catalog_product_default_order"]
    assert "COALESCE(last_change_at, created_at) DESC" in default_order and "id DESC" in default_order
    assert definitions["catalog_product_new"].endswith("(is_new_from_billz) WHERE is_new_from_billz")
    assert "(category_id, status)" in definitions["catalog_product_cat_status"]
    assert index_definitions("catalog_productstock")["catalog_stock_out"].endswith("WHERE (quantity <= 0)")
    assert '("position", id)' in index_definitions("catalog_category")["catalog_category_position"]
    assert '("position", id)' in index_definitions("catalog_banner")["catalog_banner_position"]


def test_product_image_position_unique_is_deferrable_initially_deferred() -> None:
    row = fetch_one(
        "SELECT condeferrable, condeferred FROM pg_constraint WHERE conname = %s",
        ["productimage_product_position_unique"],
    )
    assert row == (True, True)


@pytest.mark.parametrize(
    ("table", "names"),
    [
        (
            "catalog_category",
            ["category_slug_format", "category_slug_not_reserved", "category_name_uz_required"],
        ),
        (
            "catalog_product",
            [
                "product_billz_id_format",
                "product_price_non_negative",
                "product_billz_price_non_negative",
                "product_last_change_all_or_nothing",
                "product_price_change_has_prices",
            ],
        ),
        ("catalog_productimage", ["productimage_position_max_8", "productimage_content_type"]),
        (
            "catalog_banner",
            ["banner_title_uz_required", "banner_ends_after_starts", "banner_link_kind_targets"],
        ),
    ],
)
def test_check_constraints_exist(table: str, names: list[str]) -> None:
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT conname FROM pg_constraint WHERE conrelid = %s::regclass AND contype = 'c'", [table]
        )
        present = {row[0] for row in cursor.fetchall()}
    assert set(names) <= present


@pytest.mark.parametrize("term", ["serum", "ko'pik", "ko\u02bbpik", "ёлка"])
def test_search_uses_the_trigram_index(term: str) -> None:
    """The folded term is a constant the planner pre-evaluates: the GIN index still serves the LIKE."""
    ProductFactory.create(name_uz="Gialuron kislotali serum")
    qs = filter_products_search(Product.objects.all(), term).order_by()
    sql, params = qs.query.sql_with_params()
    with transaction.atomic(), connection.cursor() as cursor:
        cursor.execute("SET LOCAL enable_seqscan = off")
        cursor.execute("EXPLAIN " + sql, params)
        plan = "\n".join(row[0] for row in cursor.fetchall())
    assert "catalog_product_search_trgm" in plan and "Bitmap Index Scan" in plan


def test_collate_glowderma_ci_appears_only_in_the_ordering_annotation() -> None:
    """Review major 4: the rule is asserted at the source level, independent of the server version."""
    tree = ast.parse(inspect.getsource(selectors))
    collate_calls: list[tuple[str | None, ast.Call]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef):
            continue
        for inner in ast.walk(node):
            if isinstance(inner, ast.Call) and getattr(inner.func, "id", None) == "Collate":
                collate_calls.append((node.name, inner))
    module_level = [
        n for n in ast.walk(tree) if isinstance(n, ast.Call) and getattr(n.func, "id", None) == "Collate"
    ]
    assert len(module_level) == len(collate_calls) == 1, "Collate(...) must appear exactly once"
    function_name, call = collate_calls[0]
    assert function_name == "order_products"
    assert isinstance(call.args[1], ast.Constant) and call.args[1].value == "glowderma_ci"
    assert "glowderma_ci" not in inspect.getsource(selectors.filter_products_search)


def test_like_under_the_nondeterministic_collation_is_version_dependent() -> None:
    """PG 16 refuses LIKE under a non-deterministic collation, PG 18 accepts it; we rely on neither."""
    sql = "SELECT 'a' COLLATE glowderma_ci LIKE 'A'"
    if connection.pg_version < 180000:  # type: ignore[attr-defined]
        # psycopg maps FeatureNotSupported to NotSupportedError (the design's wording says ProgrammingError)
        with (
            pytest.raises((ProgrammingError, NotSupportedError), match="nondeterministic"),
            transaction.atomic(),
        ):
            fetch_one(sql)
    else:
        row = fetch_one(sql)
        assert row is not None and isinstance(row[0], bool)


# --- search vectors (01 §6.4) -------------------------------------------------------------------------------


@pytest.fixture
def catalog() -> dict[str, Product]:
    return {
        "serum": ProductFactory.create(
            id=4607, name_uz="Gialuron kislotali serum", name_ru="Сыворотка", billz_id="4607012301"
        ),
        "spf": ProductFactory.create(
            id=77,
            name_uz="Quyoshdan himoya kremi SPF 50",
            billz_name="SUN CREAM SPF50",
            billz_id="4607012401",
        ),
        "foam": ProductFactory.create(
            id=50, name_uz="", billz_name="GENTLE 100%_FOAM", billz_id="4607012388"
        ),
    }


def ids(qs: object) -> set[int]:
    return set(qs.values_list("id", flat=True))  # type: ignore[attr-defined]


def test_search_is_case_insensitive_over_the_five_columns(catalog: dict[str, Product]) -> None:
    base = Product.objects.all()
    assert ids(filter_products_search(base, "SERUM")) == {4607}
    assert ids(filter_products_search(base, "сЫвОр")) == {4607}
    assert ids(filter_products_search(base, "sun cream")) == {77}
    assert ids(filter_products_search(base, "spf 5")) == {77}  # across the "SPF 50" / "SPF50" boundary
    assert ids(filter_products_search(base, "  gentle  ")) == {50}
    assert ids(filter_products_search(base, "")) == {4607, 77, 50}
    assert ids(filter_products_search(base, "   ")) == {4607, 77, 50}


@pytest.mark.parametrize(
    "term",
    # U+0027, U+0060, U+2018, U+2019, U+02BB, U+02BC (gap CRIT-SEARCH-UZ-APOSTROPHE, decision GC-1)
    ["ko'pik", "ko`pik", "ko\u2018pik", "ko\u2019pik", "ko\u02bbpik", "ko\u02bcpik", "KOʻPIK", "chi ko'"],
)
def test_search_folds_every_uzbek_apostrophe_variant(term: str) -> None:
    foam = ProductFactory.create(name_uz="Yumshoq tozalovchi ko\u2018pik")  # stored with U+2018 (the seed)
    cream = ProductFactory.create(name_uz="Ko\u02bbz atrofi kremi", name_ru="Крем для век")
    base = Product.objects.all()
    assert ids(filter_products_search(base, term)) == {foam.pk}
    assert ids(filter_products_search(base, "ko'z atrofi")) == {cream.pk}
    assert ids(filter_products_search(base, "ko`z")) == {cream.pk}


@pytest.mark.parametrize("term", ["kopik", "KOPIK", "tozalovchi kopik", "ko''pik"])
def test_search_matches_spellings_without_the_apostrophe(term: str) -> None:
    """``feat/customer-web``: people type ``kopik`` for ``ko‘pik`` — the search key removes every apostrophe
    variant from the indexed text and from the term."""
    foam = ProductFactory.create(name_uz="Yumshoq tozalovchi ko\u2018pik")
    cream = ProductFactory.create(name_uz="Ko\u02bbz atrofi kremi", name_ru="Крем для век")
    base = Product.objects.all()
    assert ids(filter_products_search(base, term)) == {foam.pk}
    assert ids(filter_products_search(base, "koz atrofi")) == {cream.pk}
    assert ids(filter_products_search(base, "'\u2018")) == {foam.pk, cream.pk}  # apostrophes only: no filter


def test_search_folds_yo_to_ye_both_ways() -> None:
    yo = ProductFactory.create(name_uz="Krem", name_ru="Ёлочный крем с мёдом")
    ye = ProductFactory.create(name_uz="Balzam", name_ru="Бальзам с медом")
    base = Product.objects.all()
    assert ids(filter_products_search(base, "елочный")) == {yo.pk}
    assert ids(filter_products_search(base, "ЁЛОЧНЫЙ")) == {yo.pk}
    assert ids(filter_products_search(base, "мёд")) == {yo.pk, ye.pk}
    assert ids(filter_products_search(base, "мед")) == {yo.pk, ye.pk}


def test_search_escapes_like_wildcards(catalog: dict[str, Product]) -> None:
    base = Product.objects.all()
    assert ids(filter_products_search(base, "100%_foam")) == {50}
    assert ids(filter_products_search(base, "%")) == {50}
    assert ids(filter_products_search(base, "_")) == {50}
    assert ids(filter_products_search(base, "\\")) == set()


def test_numeric_term_matches_id_and_billz_id(catalog: dict[str, Product]) -> None:
    base = Product.objects.all()
    assert ids(filter_products_search(base, "4607")) == {4607, 77, 50}  # id 4607 + every billz_id prefix
    assert ids(filter_products_search(base, "77")) == {77}  # id 77 (no billz_id contains "77")
    assert ids(filter_products_search(base, "4607012388")) == {50}
    assert ids(filter_products_search(base, "99")) == set()


def test_non_ascii_and_oversized_digit_terms_never_raise(catalog: dict[str, Product]) -> None:
    """``str.isdigit()`` accepts superscripts and circled digits ``int()`` rejects; input must never raise."""
    base = Product.objects.all()
    for term in ("²", "①", "⑩", "٣", "4607²"):
        assert ids(filter_products_search(base, term)) == set()
    assert ids(filter_products_search(base, "9" * 19)) == set()  # beyond any bigint id: text match only
    assert ids(filter_products_search(base, "9" * 64)) == set()
    assert ids(filter_products_search(base, "0" * 17 + "77")) == set()  # 19 digits: not parsed as an id
    assert ids(filter_products_search(base, "0" * 16 + "77")) == {77}  # 18 digits: still an exact id match


def test_search_term_is_trimmed_to_64_characters(catalog: dict[str, Product]) -> None:
    term = "gialuron kislotali serum" + "x" * 80
    assert ids(filter_products_search(Product.objects.all(), term)) == set()
    serum = catalog["serum"]
    serum.refresh_from_db()
    term = serum.search_text[:64] + "zzz"  # the overflow is cut before matching
    assert ids(filter_products_search(Product.objects.all(), term)) == {4607}


# --- ordering vectors (01 §6.5) -----------------------------------------------------------------------------


def test_name_ordering_uses_display_name_and_the_ci_collation() -> None:
    names = [
        ("zink krem", "4607012001"),
        ("Aloe gel", "4607012002"),
        ("Ёлка", "4607012003"),
        ("", "4607012004"),  # display name falls back to billz_name
        ("ánd", "4607012005"),
        ("ZINK KREM", "4607012006"),
        ("ёлка", "4607012007"),
        ("Bakuchiol", "4607012008"),
    ]
    rows = [
        ProductFactory.create(name_uz=name, billz_name="BAKUCHIOL BALM" if not name else "X", billz_id=billz)
        for name, billz in names
    ]
    by_id = {row.billz_id: row for row in rows}
    ordered = list(order_products(Product.objects.all(), "name"))
    labels = [p.display_name for p in ordered]
    assert labels == [
        "Aloe gel",
        "ánd",
        "Bakuchiol",
        "BAKUCHIOL BALM",
        "zink krem",
        "ZINK KREM",
        "Ёлка",
        "ёлка",
    ]
    # equal under the collation → id ascending; `-name` is the exact reverse
    assert ordered[4:6] == [by_id["4607012001"], by_id["4607012006"]]
    assert list(order_products(Product.objects.all(), "-name")) == ordered[::-1]


def test_default_ordering_prefers_last_change_then_created_then_id() -> None:
    def created(day: int) -> datetime:
        return datetime(2026, 10, day, 9, 0, tzinfo=TASHKENT)

    old_edited = ProductFactory.create(
        created_at=created(1), last_change_at=created(20), last_change_kind=enums.ProductChangeKind.UPDATED
    )
    newer = ProductFactory.create(created_at=created(10))
    older = ProductFactory.create(created_at=created(5))
    same_day = ProductFactory.create(created_at=created(5))
    assert list(order_products(Product.objects.all(), "default")) == [old_edited, newer, same_day, older]
    assert list(order_products(Product.objects.all(), "unknown")) == [old_edited, newer, same_day, older]
