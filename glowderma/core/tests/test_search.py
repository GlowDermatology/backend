"""``core.search``: the fold every name search applies (gap CRIT-SEARCH-UZ-APOSTROPHE, decision GC-1)."""

import pytest
from django.db import connection

from glowderma.core import search
from glowderma.core.search import SearchFold, fold

# U+0027, U+0060, U+2018, U+2019, U+02BB, U+02BC: the six encodings of the Uzbek o‘ / g‘ mark
VARIANTS = ("ko'pik", "ko`pik", "ko‘pik", "ko’pik", "koʻpik", "koʼpik")


def db_fold(text: str) -> str:
    """The SQL of :class:`SearchFold` evaluated by PostgreSQL on ``text``."""
    sql = SearchFold.template % {"function": SearchFold.function, "expressions": "%s"}
    with connection.cursor() as cursor:
        cursor.execute(f"SELECT {sql}", [text])
        row = cursor.fetchone()
    assert row is not None
    return str(row[0])


def test_fold_maps_every_apostrophe_variant_and_yo() -> None:
    assert {fold(variant) for variant in VARIANTS} == {"ko'pik"}
    assert fold("ЁЛКА ёлка") == "елка елка"
    assert fold("O‘ZBEKISTON GʻUZAL") == "o'zbekiston g'uzal"
    assert len(search.FOLD_FROM) == len(search.FOLD_TO) == 7


def test_the_sql_template_carries_its_constants_as_literals() -> None:
    """The translate arguments are SQL literals, never parameters: the generated column, the expression index
    and every query then render the same expression whatever the driver's parameter binding."""
    assert "'" in search.FOLD_TO and "%" not in search.FOLD_FROM + search.FOLD_TO
    assert SearchFold.template.startswith("%(function)s(lower(%(expressions)s), '")
    assert "'ее''''''''''')" in SearchFold.template


@pytest.mark.django_db
@pytest.mark.parametrize("text", [*VARIANTS, "KOʻPIK", "Ёлка", "ёжик Ёж", "G‘ulomova", "plain text 100%"])
def test_postgres_folds_like_the_python_mirror(text: str) -> None:
    assert db_fold(text) == fold(text)
