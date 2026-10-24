from typing import Any

from rest_framework.permissions import AllowAny
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.test import APIRequestFactory
from rest_framework.views import APIView

from glowderma.core.api.pagination import EnvelopePagination


class PaginatedView(APIView):
    authentication_classes = []
    permission_classes = [AllowAny]
    page_size_options = (10, 25)
    default_page_size = 10
    items: list[int] = list(range(25))

    def get(self, request: Request) -> Response:
        paginator = EnvelopePagination()
        page = paginator.paginate_queryset(self.items, request, view=self)
        return paginator.get_paginated_response(page)


def paginated_response(params: dict[str, str] | None = None, *, empty: bool = False) -> Response:
    kwargs: dict[str, Any] = {"items": []} if empty else {}
    return PaginatedView.as_view(**kwargs)(APIRequestFactory().get("/api/test/", params))


def test_page_clamped_to_last() -> None:
    response = paginated_response({"page": "99"})
    assert response.status_code == 200
    assert response.data == {
        "count": 25,
        "page": 3,
        "page_size": 10,
        "page_count": 3,
        "results": [20, 21, 22, 23, 24],
    }


def test_non_integer_page_is_one() -> None:
    response = paginated_response({"page": "nonsense"})
    assert response.data["page"] == 1
    assert response.data["results"] == list(range(10))


def test_page_below_one_is_clamped() -> None:
    assert paginated_response({"page": "-4"}).data["page"] == 1


def test_empty_list_is_page_one() -> None:
    response = paginated_response({"page": "99"}, empty=True)
    assert response.data == {"count": 0, "page": 1, "page_size": 10, "page_count": 1, "results": []}


def test_page_size_not_allowed_is_invalid_choice() -> None:
    response = paginated_response({"page_size": "7"})
    assert response.status_code == 400
    assert response.data["code"] == "validation_error"
    assert response.data["errors"] == [
        {"field": "page_size", "code": "invalid_choice", "message": "Ruxsat etilmagan qiymat."}
    ]


def test_non_integer_page_size_is_invalid_choice() -> None:
    response = paginated_response({"page_size": "nonsense"})
    assert response.status_code == 400
    assert response.data["errors"][0]["code"] == "invalid_choice"


def test_allowed_page_size_is_used() -> None:
    response = paginated_response({"page_size": "25"})
    assert response.data["page_size"] == 25
    assert response.data["page_count"] == 1
    assert len(response.data["results"]) == 25


def test_pagination_schema_matches_envelope_and_view_options() -> None:
    paginator = EnvelopePagination()
    schema = paginator.get_paginated_response_schema({"type": "array", "items": {"type": "integer"}})
    assert schema["type"] == "object"
    assert set(schema["properties"]) == {"count", "page", "page_size", "page_count", "results"}
    assert set(schema["required"]) == {"count", "page", "page_size", "page_count", "results"}
    parameters = {item["name"]: item for item in paginator.get_schema_operation_parameters(PaginatedView())}
    assert set(parameters) == {"page", "page_size"}
    assert parameters["page_size"]["schema"]["enum"] == [10, 25]
