"""The admin customers list (admin contract §2.6 ``GET customers/``): search, the orderings and the page
query."""

from __future__ import annotations

import re

from django.db.models import F, OrderBy, Q, QuerySet, Value
from django.db.models.functions import Collate
from django.db.models.lookups import Contains

from glowderma.accounts.models import Customer
from glowderma.core.search import SearchFold
from glowderma.customers.selectors import common
from glowderma.loyalty import selectors as loyalty_selectors
from glowderma.notifications import selectors as notifications_selectors
from glowderma.notifications.models import CustomerSegment

SEARCH_MAX_LENGTH = 64


PHONE_SEARCH_MIN_DIGITS = 3


EMAIL_SEARCH_MIN_LENGTH = 3


DEFAULT_ORDERING = "-registered_at"


def _name_ci() -> Collate:
    return Collate(F("name"), common.NAME_COLLATION)


# ``ordering`` value → ORDER BY terms; ties by ``id`` in the same direction (design 03 §6.3).
ORDERINGS: dict[str, tuple[OrderBy, ...]] = {
    "-registered_at": (F("registered_at").desc(), F("id").desc()),
    "registered_at": (F("registered_at").asc(), F("id").asc()),
    "name": (_name_ci().asc(), F("id").asc()),
    "-name": (_name_ci().desc(), F("id").desc()),
    "lifetime_total": (F("lifetime_total").asc(), F("id").asc()),
    "-lifetime_total": (F("lifetime_total").desc(), F("id").desc()),
    "balance": (F("balance").asc(), F("id").asc()),
    "-balance": (F("balance").desc(), F("id").desc()),
}


def search_q(term: str) -> Q | None:
    """``name`` contains the term (case-insensitive and folded — apostrophe variants and ё, ``core.search``,
    decision GC-1: ``fold(name) LIKE '%' || fold(term) || '%'``, served by the trigram index
    ``accounts_customer_fold_trgm``), or, when the term carries at least three digits, the phone contains
    those digits, or (hybrid identity) the e-mail address contains the term (case-insensitive). Blank → no
    filter."""
    text = term.strip()
    if not text:
        return None
    condition = Q(Contains(SearchFold(F("name")), SearchFold(Value(text))))
    digits = re.sub(r"\D", "", text)
    if len(digits) >= PHONE_SEARCH_MIN_DIGITS:
        condition |= Q(phone__contains=digits)
    if len(text) >= EMAIL_SEARCH_MIN_LENGTH:
        condition |= Q(email__contains=text.lower())  # stored lower case
    return condition


def customers_list(
    *,
    search: str | None = None,
    status: str | None = None,
    segment_id: int | None = None,
    ordering: str = DEFAULT_ORDERING,
) -> QuerySet[Customer]:
    """The filtered, ordered customer rows annotated with ``lifetime_total``, ``balance``,
    ``online_orders_count`` and ``percent`` (the active policy's rate for ``lifetime_total`` | ``None``)."""
    if ordering not in ORDERINGS:
        raise ValueError(f"Unknown customers ordering {ordering!r}")
    qs = Customer.objects.annotate(**loyalty_selectors.list_annotations())
    if status is not None:
        qs = qs.filter(status=status)
    if search is not None:
        condition = search_q(search)
        if condition is not None:
            qs = qs.filter(condition)
    if segment_id is not None:
        segment = CustomerSegment.objects.filter(pk=segment_id).first()
        if segment is None:
            return qs.none()
        qs = qs.filter(pk__in=notifications_selectors.segment_member_ids(segment))
    return qs.order_by(*ORDERINGS[ordering])


__all__ = [
    "DEFAULT_ORDERING",
    "EMAIL_SEARCH_MIN_LENGTH",
    "ORDERINGS",
    "PHONE_SEARCH_MIN_DIGITS",
    "SEARCH_MAX_LENGTH",
    "customers_list",
    "search_q",
]
