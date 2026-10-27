"""Fix pass 1 regressions: the generated OpenAPI of the P1 customer routes matches the contract shapes."""

from __future__ import annotations

from typing import Any

import pytest
from drf_spectacular.generators import SchemaGenerator


@pytest.fixture(scope="module")
def operations(django_db_blocker: Any) -> dict[str, dict[str, Any]]:
    with django_db_blocker.unblock():
        schema = SchemaGenerator().get_schema(request=None, public=True)  # type: ignore[no-untyped-call]
    found: dict[str, dict[str, Any]] = {}
    for item in schema["paths"].values():
        for operation in item.values():
            if isinstance(operation, dict) and "operationId" in operation:
                found[operation["operationId"]] = operation
    found["__components__"] = schema["components"]["schemas"]
    return found


def test_catalog_products_list_answers_the_page_envelope(operations: dict[str, dict[str, Any]]) -> None:
    operation = operations["customer_catalog_products_list"]
    ref = operation["responses"]["200"]["content"]["application/json"]["schema"]["$ref"]
    component = operations["__components__"][ref.rsplit("/", 1)[-1]]
    assert component["type"] == "object"
    assert set(component["required"]) == {"count", "page", "page_size", "page_count", "results"}
    assert component["properties"]["results"]["items"]["$ref"].endswith("/ProductCard")
    names = [parameter["name"] for parameter in operation["parameters"]]
    assert names.count("page") == 1 and names.count("page_size") == 1
    page_size = next(p for p in operation["parameters"] if p["name"] == "page_size")
    assert page_size["schema"]["enum"] == [20, 50]


@pytest.mark.parametrize("operation_id", ["customer_me_destroy", "customer_me_social_unlink"])
def test_delete_routes_document_the_required_reauth_body(
    operations: dict[str, dict[str, Any]], operation_id: str
) -> None:
    body = operations[operation_id]["requestBody"]
    assert body["required"] is True
    ref = body["content"]["application/json"]["schema"]["$ref"]
    component = operations["__components__"][ref.rsplit("/", 1)[-1]]
    assert set(component["properties"]) == {"password", "verification_token"}


def test_delete_image_keeps_no_request_body(operations: dict[str, dict[str, Any]]) -> None:
    assert "requestBody" not in operations["customer_me_image_destroy"]
