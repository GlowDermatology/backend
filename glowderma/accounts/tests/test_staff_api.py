"""Contract §2.16 ``GET staff/``: the staff directory behind the audit-log actor filter."""

from typing import Any

import pytest
from rest_framework.test import APIClient

from glowderma.accounts.models import StaffUser
from glowderma.accounts.tests.factories import StaffUserFactory

pytestmark = pytest.mark.django_db

URL = "/api/v1/admin/staff/"


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
    assert body["results"] == [
        {
            "id": no_name.pk,
            "display_name": "kassa@glowderma.uz",
            "email": "kassa@glowderma.uz",
            "is_active": True,
        },
        {
            "id": nodira.pk,
            "display_name": "nodira Rahimova",
            "email": "nodira@glowderma.uz",
            "is_active": True,
        },
        {"id": staff.pk, "display_name": staff.full_name, "email": staff.email, "is_active": True},
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
