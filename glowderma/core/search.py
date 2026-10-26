"""Search folding shared by every name search (gap CRIT-SEARCH-UZ-APOSTROPHE, decision GC-1).

Uzbek Latin writes o‘ and g‘ with an apostrophe-like mark that phones and keyboards encode six ways: the
ASCII apostrophe U+0027, the backtick U+0060, U+2018 / U+2019 (typographic quotes) and U+02BB / U+02BC
(modifier letters). Russian users type ``е`` for ``ё``. A search therefore compares **folded** text on both
sides: ``translate(lower(text), FOLD_FROM, FOLD_TO)`` (``Ёё`‘’ʻʼ`` → ``ее'''''``) — lower-cased, ``ё`` →
``е`` and every apostrophe variant → U+0027. ``ko'pik``, ``ko‘pik``, ``koʻpik`` and ``ko`pik`` are then one
word, and so are ``ёлка`` and ``елка``.

:class:`SearchFold` is that SQL expression. It is IMMUTABLE (``lower`` and ``translate`` are), so it may
define a stored generated column (``catalog.Product.search_text``) and an expression index
(``accounts.Customer`` ``accounts_customer_fold_trgm``), and the planner pre-evaluates it on a constant
search term — the trigram GIN indexes keep serving ``LIKE '%…%'``. The two translate arguments are SQL
literals in the template (never parameters), so the column, the index and every query render the very same
expression whatever the driver's parameter binding. :func:`fold` mirrors it in Python for tests and logs; the
database expression is the authority (PostgreSQL's ``lower`` and Python's ``str.lower`` differ on a few
letters).
"""

from django.db.models import Func, TextField

# The target apostrophe (U+0027) and the five other encodings folded onto it.
APOSTROPHE = "'"
APOSTROPHE_VARIANTS = "`\u2018\u2019\u02bb\u02bc"  # ` ‘ ’ ʻ ʼ (U+0060, U+2018, U+2019, U+02BB, U+02BC)
FOLD_FROM = "\u0401\u0451" + APOSTROPHE_VARIANTS  # Ё ё, then the apostrophes
FOLD_TO = "\u0435\u0435" + APOSTROPHE * len(APOSTROPHE_VARIANTS)  # Cyrillic е е, then U+0027 each
_TABLE = str.maketrans(FOLD_FROM, FOLD_TO)


def _sql_literal(text: str) -> str:
    """A standard-conforming SQL string literal (quotes doubled; ``%`` cannot occur in the constants)."""
    if "%" in text or "\\" in text:  # pragma: no cover - guarded by a test; would break the SQL template
        raise ValueError("fold constants must not contain % or a backslash")
    return "'" + text.replace("'", "''") + "'"


class SearchFold(Func):
    """``translate(lower(<expression>), FOLD_FROM, FOLD_TO)`` as SQL literals — see the module doc."""

    function = "translate"
    template = f"%(function)s(lower(%(expressions)s), {_sql_literal(FOLD_FROM)}, {_sql_literal(FOLD_TO)})"
    arity = 1
    output_field = TextField()


def fold(text: str) -> str:
    """The Python mirror of :class:`SearchFold` (tests, logs)."""
    return text.lower().translate(_TABLE)


__all__ = ["APOSTROPHE", "APOSTROPHE_VARIANTS", "FOLD_FROM", "FOLD_TO", "SearchFold", "fold"]
