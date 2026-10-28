"""Contract §2.6 ``GET customers/``: item shape, every ordering against the design 03 §6.3 SQL (collation and
ties by id included), search vectors, status and segment filters, the page envelope, the export and a bounded
number of queries."""

from __future__ import annotations

import csv
import io
from datetime import timedelta
from typing import Any

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext
from rest_framework.test import APIClient

from glowderma.accounts.models import Customer, StaffUser
from glowderma.core.models import AuditEntry
from glowderma.customers.tests.conftest import ANCHOR
from glowderma.customers.tests.helpers import (
    SQL_ORDER_BY,
    make_customer,
    online_purchase,
    set_balance,
    sql_order,
    store_purchase,
)
from glowderma.loyalty.models import CashbackPolicy
from glowderma.notifications.tests.factories import CustomerSegmentFactory, SegmentMemberFactory

pytestmark = pytest.mark.django_db

URL = "/api/v1/admin/customers/"
MLN = 1_000_000


def ids(response: Any) -> list[int]:
    assert response.status_code == 200, response.data
    return [row["id"] for row in response.json()["results"]]


def get_all(client: APIClient, **params: str | int) -> list[int]:
    return ids(client.get(URL, {"page_size": 50, **params}))


@pytest.fixture
def population(staff: StaffUser, policies: dict[int, CashbackPolicy]) -> list[Customer]:
    """Mixed Latin/Cyrillic names (with case-only duplicates for collation ties), equal registration times,
    lifetime totals and balances (ties broken by id), a deleted customer with a scrubbed name."""
    rows = [
        ("Zarina", 5, 25 * MLN, 1_000),
        ("алина", 9, 5 * MLN, 0),
        ("Alisher", 9, 5 * MLN, 300),  # same registered_at and lifetime as алина
        ("Бахтиёр", 2, 0, 300),  # same balance as Alisher
        ("anvar", 7, 60 * MLN, -500),
        ("Anvar", 3, 0, 0),  # equal to "anvar" under glowderma_ci → tie by id
        ("Ёқуб", 12, 13_580_000, 48_600),
        ("ägnes", 4, 1_000, 0),
    ]
    customers = []
    for name, days_ago, lifetime, balance in rows:
        customer = make_customer(name, days_ago=days_ago)
        if lifetime:
            store_purchase(customer, lifetime)
        if balance:
            set_balance(customer, balance, staff)
        customers.append(customer)
    deleted = make_customer("", days_ago=1, status="deleted")
    store_purchase(deleted, 700_000)
    reversed_only = make_customer("Reversed", days_ago=6)
    store_purchase(reversed_only, 9 * MLN, reversed=True, reversed_at=ANCHOR)
    return [*customers, deleted, reversed_only]


@pytest.mark.parametrize("ordering", list(SQL_ORDER_BY))
def test_every_ordering_matches_the_design_sql(
    staff_client: APIClient, population: list[Customer], ordering: str
) -> None:
    assert get_all(staff_client, ordering=ordering) == sql_order(ordering)


def test_default_ordering_is_newest_registration_first(
    staff_client: APIClient, population: list[Customer]
) -> None:
    assert get_all(staff_client) == sql_order("-registered_at")


def test_name_ordering_uses_the_case_insensitive_collation_and_ties_by_id(
    staff_client: APIClient, population: list[Customer]
) -> None:
    by_name = {c.pk: c.name for c in population}
    names = [by_name[pk] for pk in get_all(staff_client, ordering="name")]
    # ICU root order: "" first, Latin (accents secondary, case ignored) before Cyrillic
    assert names == [
        "",
        "ägnes",
        "Alisher",
        "anvar",
        "Anvar",
        "Reversed",
        "Zarina",
        "алина",
        "Бахтиёр",
        "Ёқуб",
    ]
    anvar_lower, anvar_upper = population[4], population[5]
    assert anvar_lower.pk < anvar_upper.pk  # "anvar" == "Anvar" under the collation: ties by id ascending
    names_desc = [by_name[pk] for pk in get_all(staff_client, ordering="-name")]
    assert names_desc.index("Anvar") < names_desc.index("anvar")  # descending ties: id descending


def test_lifetime_and_balance_orderings_are_server_side_across_pages(
    staff_client: APIClient, staff: StaffUser, population: list[Customer]
) -> None:
    for index in range(25):  # 35 rows: two pages of 20, with repeated totals and balances (ties by id)
        customer = make_customer(f"Qo‘shimcha {index}", days_ago=index % 4)
        store_purchase(customer, (index % 6) * 1_500_000 + 1)
        set_balance(customer, (index % 5) * 100 + 1, staff)
    for ordering in ("-lifetime_total", "lifetime_total", "-balance", "balance"):
        first = ids(staff_client.get(URL, {"ordering": ordering, "page": "1"}))
        second = ids(staff_client.get(URL, {"ordering": ordering, "page": "2"}))
        assert first + second == sql_order(ordering), ordering
    rows = {row["id"]: row for row in staff_client.get(URL, {"page_size": "50"}).json()["results"]}
    balances = [rows[pk]["balance"] for pk in sql_order("balance")]
    assert balances == sorted(balances) and balances[0] == -500
    totals = [rows[pk]["lifetime_total"] for pk in sql_order("-lifetime_total")]
    assert totals == sorted(totals, reverse=True) and totals[0] == 60 * MLN


def test_item_shape(staff_client: APIClient, staff: StaffUser, policies: dict[int, CashbackPolicy]) -> None:
    malika = make_customer("Malika Karimova", phone="+998901234567")
    store_purchase(malika, 9_940_000)
    online_purchase(malika, 3_000_000)
    online_purchase(malika, 640_000)
    online_purchase(malika, 500_000, reversed=True, reversed_at=ANCHOR)  # never counts
    set_balance(malika, 48_600, staff)
    silver = make_customer("Silver")
    store_purchase(silver, 25 * MLN)
    beyond = make_customer("Beyond")
    store_purchase(beyond, 50 * MLN)  # v3's top band is bounded at 50 mln → no tier
    rows = {row["id"]: row for row in staff_client.get(URL).json()["results"]}
    assert rows[malika.pk] == {
        "id": malika.pk,
        "name": "Malika Karimova",
        "phone": "+998901234567",
        "email": None,
        "status": "active",
        "registered_at": (ANCHOR - timedelta(days=30)).isoformat(),
        "lifetime_total": 13_580_000,
        "percent": "1.00",
        "balance": 48_600,
        "online_orders_count": 2,
    }
    assert rows[silver.pk]["percent"] == "2.00" and rows[silver.pk]["balance"] == 0
    assert rows[beyond.pk]["percent"] is None
    assert rows[beyond.pk]["online_orders_count"] == 0


def test_percent_is_null_without_an_active_policy(staff_client: APIClient, clock: Any) -> None:
    customer = make_customer("No policy")
    store_purchase(customer, 1_000)
    assert staff_client.get(URL).json()["results"][0]["percent"] is None


@pytest.mark.parametrize(
    ("term", "expected"),
    [
        ("mal", {"Malika Karimova", "Kamalova"}),  # name contains, case-insensitive
        ("MALIKA", {"Malika Karimova"}),
        ("алИ", {"Алишер"}),  # Cyrillic case folding
        ("  karim  ", {"Malika Karimova"}),  # trimmed
        ("12", set()),  # fewer than 3 digits never reach the phone
        ("123", {"Malika Karimova", "Алишер"}),
        ("+998 90 123", {"Malika Karimova"}),  # the digits of the term: 99890123
        ("97 555", {"Kamalova"}),
        ("%", set()),  # LIKE wildcards are literal
        ("_", set()),
        ("", {"Malika Karimova", "Kamalova", "Алишер", ""}),  # blank → no filter
    ],
)
def test_search_rule_vectors(
    staff_client: APIClient, policies: dict[int, CashbackPolicy], term: str, expected: set[str]
) -> None:
    make_customer("Malika Karimova", phone="+998901234567")
    make_customer("Kamalova", phone="+998975551122")
    make_customer("Алишер", phone="+998931239876")
    make_customer("", phone="+998940000000", status="deleted")
    rows = staff_client.get(URL, {"search": term}).json()["results"]
    assert {row["name"] for row in rows} == expected


@pytest.mark.parametrize(
    "term",
    # gap CRIT-SEARCH-UZ-APOSTROPHE (GC-1): U+0027, U+0060, U+2018, U+2019, U+02BB, U+02BC and ё → е
    ["g'ulom", "g`ulom", "g\u2018ulom", "g\u2019ulom", "G\u02bbULOM", "g\u02bculom", "ахмедова", "ахмёдова"],
)
def test_search_folds_apostrophes_and_yo_in_names(
    staff_client: APIClient, policies: dict[int, CashbackPolicy], term: str
) -> None:
    make_customer("Dilnoza G\u2018ulomova", phone="+998901234567")  # typed with U+2018 at registration
    make_customer("Мадина Ахмёдова", phone="+998901234568")
    make_customer("Malika Karimova", phone="+998901234569")
    rows = staff_client.get(URL, {"search": term}).json()["results"]
    expected = "Мадина Ахмёдова" if term.startswith("ахм") else "Dilnoza G\u2018ulomova"
    assert [row["name"] for row in rows] == [expected]


def test_search_is_at_most_64_characters(staff_client: APIClient, clock: Any) -> None:
    response = staff_client.get(URL, {"search": "x" * 65})
    assert response.status_code == 400
    assert [(e["field"], e["code"]) for e in response.data["errors"]] == [("search", "max_length")]
    assert staff_client.get(URL, {"search": "x" * 64}).status_code == 200


def test_status_filter(staff_client: APIClient, policies: dict[int, CashbackPolicy]) -> None:
    active = make_customer("Faol")
    deleted = make_customer("", status="deleted")
    assert get_all(staff_client, status="active") == [active.pk]
    assert get_all(staff_client, status="deleted") == [deleted.pk]
    deleted_row = staff_client.get(URL, {"status": "deleted"}).json()["results"][0]
    assert deleted_row["name"] == "" and deleted_row["status"] == "deleted"
    bad = staff_client.get(URL, {"status": "blocked"})
    assert bad.status_code == 400 and bad.data["errors"][0]["field"] == "status"
    assert bad.data["errors"][0]["code"] == "invalid_choice"


def test_segment_filter_uses_the_membership_rule(
    staff_client: APIClient, policies: dict[int, CashbackPolicy]
) -> None:
    big = make_customer("Big")
    store_purchase(big, 12 * MLN)
    small = make_customer("Small")
    store_purchase(small, 1 * MLN)
    manual = make_customer("Manual")
    deleted_big = make_customer("", status="deleted")
    store_purchase(deleted_big, 20 * MLN)
    segment = CustomerSegmentFactory.create(min_online_orders=None, min_lifetime_amount=10 * MLN)
    SegmentMemberFactory.create(segment=segment, customer=manual)
    SegmentMemberFactory.create(segment=segment, customer=deleted_big)  # deleted customers never count
    assert set(get_all(staff_client, segment=segment.pk)) == {big.pk, manual.pk}
    assert get_all(staff_client, segment=segment.pk, search="man") == [manual.pk]
    assert get_all(staff_client, segment=segment.pk + 100) == []  # unknown segment: nobody
    assert small.pk not in get_all(staff_client, segment=segment.pk)
    bad = staff_client.get(URL, {"segment": "x"})
    assert bad.status_code == 400 and bad.data["errors"][0]["field"] == "segment"


def test_page_envelope(staff_client: APIClient, policies: dict[int, CashbackPolicy]) -> None:
    for index in range(23):
        make_customer(f"Mijoz {index:02d}", days_ago=index + 1)
    first = staff_client.get(URL).json()
    assert {k: v for k, v in first.items() if k != "results"} == {
        "count": 23,
        "page": 1,
        "page_size": 20,
        "page_count": 2,
    }
    assert len(first["results"]) == 20
    last = staff_client.get(URL, {"page": 99}).json()
    assert last["page"] == 2 and len(last["results"]) == 3
    assert staff_client.get(URL, {"page": "x"}).json()["page"] == 1
    assert staff_client.get(URL, {"page_size": 50}).json()["page_size"] == 50
    bad = staff_client.get(URL, {"page_size": 10})
    assert bad.status_code == 400
    assert [(e["field"], e["code"]) for e in bad.data["errors"]] == [("page_size", "invalid_choice")]
    assert staff_client.get(URL, {"ordering": "phone"}).status_code == 400


def test_query_count_does_not_grow_with_the_rows(
    staff_client: APIClient, staff: StaffUser, policies: dict[int, CashbackPolicy]
) -> None:
    def count_queries() -> int:
        with CaptureQueriesContext(connection) as context:
            assert staff_client.get(URL, {"ordering": "-lifetime_total"}).status_code == 200
        return len(context.captured_queries)

    customer = make_customer("Birinchi")
    store_purchase(customer, 1_000)
    count_queries()  # the first authenticated call also touches the session row
    few = count_queries()
    for index in range(15):
        customer = make_customer(f"Mijoz {index}")
        store_purchase(customer, 1_000 * index + 1)
        online_purchase(customer)
        set_balance(customer, index + 1, staff)
    assert count_queries() == few
    # auth (session + staff), the active policy (version, prefetch, tiers), count and the page
    assert few <= 8


def test_csv_export(staff_client: APIClient, staff: StaffUser, policies: dict[int, CashbackPolicy]) -> None:
    malika = make_customer("Malika Karimova", phone="+998901234567")
    store_purchase(malika, 13_580_000)
    set_balance(malika, 48_600, staff)
    formula = make_customer("=HYPERLINK(1)", phone="+998901234568", days_ago=40)
    deleted = make_customer("", phone="+998901234569", status="deleted", days_ago=50)
    response = staff_client.get(URL, {"export": "csv", "page_size": "20"})
    assert response.status_code == 200
    assert response["Content-Type"] == "text/csv; charset=utf-8"
    assert response["Content-Disposition"] == 'attachment; filename="mijozlar-20261024-1205.csv"'
    content = response.content.decode("utf-8")
    assert content.startswith("﻿")
    rows = list(csv.reader(io.StringIO(content.lstrip("﻿")), delimiter=";"))
    assert rows[0] == [
        "ID",
        "Ism",
        "Telefon",
        "Ro‘yxatdan o‘tgan",
        "Jami xaridlar (so‘m)",
        "Cashback foizi",
        "Balans (so‘m)",
        "Holat",
    ]
    assert rows[1:] == [
        [
            str(malika.pk),
            "Malika Karimova",
            "+998901234567",
            "2026-09-24 12:05",
            "13580000",
            "1.00",
            "48600",
            "Faol",
        ],
        [str(formula.pk), "'=HYPERLINK(1)", "+998901234568", "2026-09-14 12:05", "0", "1.00", "0", "Faol"],
        [str(deleted.pk), "", "+998901234569", "2026-09-04 12:05", "0", "1.00", "0", "O‘chirilgan"],
    ]
    audit = AuditEntry.objects.get(action="export.downloaded")
    assert audit.after == {"resource": "customers", "filters": {}, "row_count": 3, "format": "csv"}


def test_xlsx_export_keeps_filters_and_ordering(
    staff_client: APIClient, policies: dict[int, CashbackPolicy]
) -> None:
    make_customer("Bekzod")
    make_customer("", status="deleted")
    response = staff_client.get(URL, {"export": "xlsx", "status": "active", "ordering": "name"})
    assert response.status_code == 200
    assert response["Content-Type"] == "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    assert response["Content-Disposition"].endswith('.xlsx"')
    audit = AuditEntry.objects.get(action="export.downloaded")
    assert audit.after is not None
    assert audit.after["filters"] == {"status": "active", "ordering": "name"}
    assert audit.after["row_count"] == 1
    assert staff_client.get(URL, {"export": "pdf"}).status_code == 400


def test_export_audit_ignores_unknown_parameters_with_nul(
    staff_client: APIClient, policies: dict[int, CashbackPolicy]
) -> None:
    """An unknown parameter carrying NUL used to reach the JSONB audit ``filters`` and answer 500."""
    make_customer("Bekzod")
    response = staff_client.get(URL + "?export=csv&foo=%00&status=active&ordering=name")
    assert response.status_code == 200
    audit = AuditEntry.objects.get(action="export.downloaded")
    assert audit.after is not None
    assert audit.after["filters"] == {"status": "active", "ordering": "name"}
    assert audit.after["row_count"] == 1


def test_known_parameter_with_nul_is_400(
    staff_client: APIClient, policies: dict[int, CashbackPolicy]
) -> None:
    response = staff_client.get(URL + "?export=csv&search=a%00b")
    assert response.status_code == 400
    assert response.data["errors"][0]["field"] == "search"
    assert not AuditEntry.objects.filter(action="export.downloaded").exists()
