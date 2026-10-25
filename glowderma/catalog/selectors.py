"""Read-side helpers of `catalog` (design 01 §6.3–6.5). B3 adds the list/stat selectors on top of these.

``glowderma_ci`` (the ICU case-insensitive collation from core.0007) is used **only** inside the ordering
annotation of :func:`order_products` — never in ``LIKE``, equality, ``DISTINCT``, ``GROUP BY`` or a column's
``db_collation`` (a test walks this module's AST to enforce it). Search goes through the stored, deterministic
``search_text`` column so the trigram GIN index applies.
"""

from django.db.models import F, Q, QuerySet, Value
from django.db.models.functions import Coalesce, Collate, Lower, NullIf

from glowderma.catalog.models import Product

SEARCH_TERM_MAX_LENGTH = 64
MAX_ID_DIGITS = 18  # every 18-digit number fits a bigint id; longer terms can only match the text columns

# Display name = Uzbek name, else the Billz name (what every Uzbek label of a product shows).
DISPLAY_NAME = Coalesce(NullIf(F("name_uz"), Value("")), F("billz_name"))


def filter_products_search(qs: QuerySet[Product], raw: str) -> QuerySet[Product]:
    """Contract §2.3 ``search``: case-insensitive substring over five columns; digits also match ``id``."""
    term = raw.strip()[:SEARCH_TERM_MAX_LENGTH]
    if not term:
        return qs
    # LIKE '%' || lower('term') || '%' — both sides lower-cased by PostgreSQL (same lower() as the generated
    # column); Django escapes % _ \ in the term.
    cond = Q(search_text__contains=Lower(Value(term)))
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
