"""Contract §2.16 ``GET staff/``: the staff directory behind the audit-log actor filter, and a manager's full
rows (``StaffMember`` columns)."""

from datetime import datetime
from typing import Any

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext
from rest_framework.test import APIClient

from glowderma.accounts.models import SocialIdentity, StaffUser
from glowderma.accounts.tests.factories import AuthSessionFactory, StaffUserFactory
from glowderma.core import timeutils
from glowderma.core.seeding import pinned_clock
from glowderma.core.timeutils import TASHKENT

pytestmark = pytest.mark.django_db

URL = "/api/v1/admin/staff/"
HIDDEN = dict.fromkeys(("full_name", "is_manager", "last_login_at", "linked_providers", "created_at"))


def errors(response: Any) -> list[tuple[str | None, str, str]]:
    return [(e["field"], e["code"], e["message"]) for e in response.data["errors"]]


def test_lists_active_staff_by_display_name(staff_client: APIClient, staff: StaffUser) -> None:
    nodira = StaffUserFactory.create(full_name="nodira Rahimova", email="nodira@glowderma.uz")
    no_name = StaffUserFactory.create(full_name="", email="kassa@glowderma.uz")
    StaffUserFactory.create(full_name="Avvalgi xodim", email="old@glowderma.uz", is_active=False)
    response = staff_client.get(URL)
    assert response.status_code == 200
    body = response.data
    assert (body["count"], body["page"], body["page_size"], body["page_count"]) == (3, 1, 50, 1)
    # A caller who is not a manager gets the directory keys; the manager's columns are null.
    assert body["results"] == [
        {
            "id": no_name.pk,
            "display_name": "kassa@glowderma.uz",
            "email": "kassa@glowderma.uz",
            "is_active": True,
            **HIDDEN,
        },
        {
            "id": nodira.pk,
            "display_name": "nodira Rahimova",
            "email": "nodira@glowderma.uz",
            "is_active": True,
            **HIDDEN,
        },
        {"id": staff.pk, "display_name": staff.full_name, "email": staff.email, "is_active": True, **HIDDEN},
    ]


def test_filters_search_and_is_active(staff_client: APIClient) -> None:
    StaffUserFactory.create(full_name="Nodira Rahimova", email="nodira@glowderma.uz")
    old = StaffUserFactory.create(full_name="Avvalgi xodim", email="old@glowderma.uz", is_active=False)

    def names(**params: Any) -> list[str]:
        return [r["display_name"] for r in staff_client.get(URL, params).data["results"]]

    assert names(search="  RAHIM ") == ["Nodira Rahimova"]
    assert names(search="glowderma.uz") == ["Nodira Rahimova"]
    assert names(is_active="false") == [old.full_name]
    assert names(is_active="0", search="old@") == [old.full_name]
    assert len(names(search="")) == 2  # the caller and Nodira


@pytest.mark.parametrize(
    "params,expected",
    [
        ({"page_size": 10}, [("page_size", "invalid_choice", "Ruxsat etilmagan qiymat.")]),
        ({"is_active": "yes"}, [("is_active", "invalid_choice", "Ruxsat etilmagan qiymat.")]),
        ({"search": "x" * 65}, [("search", "max_length", "64 belgidan oshmasin.")]),
    ],
)
def test_validation(staff_client: APIClient, params: dict[str, Any], expected: list[Any]) -> None:
    response = staff_client.get(URL, params)
    assert response.status_code == 400
    assert errors(response) == expected


def test_page_size_allow_list_up_to_100(staff_client: APIClient) -> None:
    StaffUserFactory.create_batch(3)
    response = staff_client.get(URL, {"page_size": 100})
    assert response.status_code == 200 and response.data["page_size"] == 100 and response.data["count"] == 4


def test_requires_a_staff_token(anon_client: APIClient) -> None:
    assert anon_client.get(URL).status_code == 401


def test_the_directory_never_reads_the_password_hash(django_assert_num_queries: Any) -> None:
    """The selector loads only the directory columns, and rendering them triggers no deferred-field query."""
    from glowderma.accounts import selectors
    from glowderma.accounts.api.admin.serializers import StaffListItemSerializer

    StaffUserFactory.create(full_name="", email="kassa@glowderma.uz")
    StaffUserFactory.create(full_name="Nodira", email="nodira@glowderma.uz")
    rows = selectors.staff_list({"is_active": True})
    sql = str(rows.query)
    assert "password" not in sql and "last_login" not in sql and "is_superuser" not in sql
    with django_assert_num_queries(1):
        data = StaffListItemSerializer(rows, many=True).data
    assert [item["display_name"] for item in data] == ["kassa@glowderma.uz", "Nodira"]


def test_a_manager_gets_the_full_rows(manager_client: APIClient, manager: StaffUser) -> None:
    with pinned_clock(datetime(2026, 10, 20, 9, 0, tzinfo=TASHKENT)):
        nodira = StaffUserFactory.create(full_name="Nodira", email="nodira@glowderma.uz")
        AuthSessionFactory.create(staff=nodira)  # an older sign-in
    with pinned_clock(datetime(2026, 10, 24, 18, 30, tzinfo=TASHKENT)):
        AuthSessionFactory.create(staff=nodira, revoked_at=timeutils.now())  # the newest, already ended
    SocialIdentity.objects.create(staff=nodira, provider="google", subject="g-1")
    SocialIdentity.objects.create(staff=nodira, provider="facebook", subject="f-1")
    rows = {row["id"]: row for row in manager_client.get(URL).data["results"]}
    assert rows[nodira.pk] == {
        "id": nodira.pk,
        "display_name": "Nodira",
        "email": "nodira@glowderma.uz",
        "is_active": True,
        "full_name": "Nodira",
        "is_manager": False,
        "last_login_at": "2026-10-24T18:30:00+05:00",
        "linked_providers": ["facebook", "google"],
        "created_at": "2026-10-20T09:00:00+05:00",
    }
    assert rows[manager.pk]["is_manager"] is True
    assert rows[manager.pk]["linked_providers"] == []
    assert rows[manager.pk]["last_login_at"] is not None  # the session behind manager_client


def test_a_manager_who_never_signed_in_has_no_last_login(manager_client: APIClient) -> None:
    fresh = StaffUserFactory.create(full_name="Yangi")
    rows = {row["id"]: row for row in manager_client.get(URL).data["results"]}
    assert rows[fresh.pk]["last_login_at"] is None


@pytest.mark.parametrize("as_manager", [False, True])
def test_list_query_count_does_not_grow_with_rows(
    as_manager: bool, staff_client: APIClient, manager_client: APIClient
) -> None:
    client = manager_client if as_manager else staff_client

    def count() -> int:
        with CaptureQueriesContext(connection) as context:
            assert client.get(URL).status_code == 200
        return len(context.captured_queries)

    client.get(URL)  # warm the session cache entry
    before = count()
    for index in range(5):
        user = StaffUserFactory.create()
        AuthSessionFactory.create(staff=user)
        SocialIdentity.objects.create(staff=user, provider="google", subject=f"g-{index}")
    assert count() == before


def test_the_manager_rows_read_no_password_hash(manager: StaffUser, django_assert_num_queries: Any) -> None:
    from glowderma.accounts import selectors
    from glowderma.accounts.api.admin.serializers import StaffListItemSerializer

    StaffUserFactory.create_batch(3)
    rows = selectors.staff_list({"is_active": True}, members=True)
    assert "password" not in str(rows.query)
    with django_assert_num_queries(2):  # the page and one prefetch of the linked providers
        data = StaffListItemSerializer(rows, many=True, context={"manager": True}).data
    assert len(data) == 4
