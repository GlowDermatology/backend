from typing import Any

from django.core.paginator import Paginator
from rest_framework.exceptions import ErrorDetail, ValidationError
from rest_framework.pagination import PageNumberPagination
from rest_framework.request import Request
from rest_framework.response import Response


class EnvelopePagination(PageNumberPagination):
    page_size_query_param = "page_size"
    page_size_options = (20, 50)
    default_page_size = 20

    def paginate_queryset(self, queryset: Any, request: Request, view: Any = None) -> list[Any]:
        self.request = request
        options = getattr(view, "page_size_options", self.page_size_options)
        default = getattr(view, "default_page_size", self.default_page_size)
        try:
            size = int(request.query_params.get("page_size", default))
        except ValueError, TypeError:
            size = 0
        if size not in options:
            raise ValidationError({"page_size": [ErrorDetail("Ruxsat etilmagan qiymat.", "invalid_choice")]})
        paginator = Paginator(queryset, size)
        try:
            number = int(request.query_params.get("page", "1"))
        except ValueError:
            number = 1
        self.page = paginator.page(max(1, min(number, paginator.num_pages)))
        return list(self.page)

    def get_paginated_response(self, data: Any) -> Response:
        if self.page is None:
            raise RuntimeError("paginate_queryset must run before creating a response")
        return Response(
            {
                "count": self.page.paginator.count,
                "page": self.page.number,
                "page_size": self.page.paginator.per_page,
                "page_count": self.page.paginator.num_pages,
                "results": data,
            }
        )

    def get_paginated_response_schema(self, schema: dict[str, Any]) -> dict[str, Any]:
        properties = {name: {"type": "integer"} for name in ("count", "page", "page_size", "page_count")}
        properties["results"] = schema
        return {"type": "object", "required": list(properties), "properties": properties}

    def get_schema_operation_parameters(self, view: Any) -> list[dict[str, Any]]:
        return [
            {"name": "page", "in": "query", "required": False, "schema": {"type": "integer", "minimum": 1}},
            {
                "name": "page_size",
                "in": "query",
                "required": False,
                "schema": {
                    "type": "integer",
                    "enum": list(getattr(view, "page_size_options", self.page_size_options)),
                },
            },
        ]
