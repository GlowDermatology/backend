"""The strict JSON parser: unpaired surrogates are a 400, paired ones (emoji) pass."""

import io
from typing import Any

import pytest
from rest_framework.exceptions import ParseError
from rest_framework.test import APIClient

from glowderma.core.api.parsers import JSONParser, has_lone_surrogate


def parse(body: str) -> Any:
    return JSONParser().parse(io.BytesIO(body.encode()), "application/json")


@pytest.mark.parametrize(
    "body",
    [
        '{"value": "\\ud800"}',
        '{"value": "\\udfff tail"}',
        '{"nested": [{"deep": ["ok", "\\udc00"]}]}',
        '{"\\ud83d": "key only"}',
        '["\\ude00\\ud83d"]',
    ],
)
def test_unpaired_surrogates_are_refused(body: str) -> None:
    with pytest.raises(ParseError):
        parse(body)


def test_paired_surrogates_decode_to_one_code_point_and_pass() -> None:
    assert parse('{"name": "Glow \\ud83d\\ude00", "list": [1, null, true, {"k": "o‘zbek"}]}') == {
        "name": "Glow \U0001f600",
        "list": [1, None, True, {"k": "o‘zbek"}],
    }
    assert not has_lone_surrogate({"a": ["b", {"c": 1}], "d": None})


@pytest.mark.django_db
def test_an_admin_write_with_a_lone_surrogate_is_a_400_envelope(staff_client: APIClient) -> None:
    response = staff_client.generic(
        "POST",
        "/api/v1/admin/categories/",
        '{"name": {"uz": "\\ud800", "ru": "x", "en": "x"}, "slug": "x"}',
        content_type="application/json",
    )
    assert response.status_code == 400
    assert response.json()["code"] == "parse_error"


def test_no_module_parses_json_with_the_stock_drf_parser() -> None:
    """Every JSON body goes through the strict parser: no module imports DRF's ``JSONParser`` itself."""
    import ast
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    offenders = []
    for path in root.rglob("*.py"):
        if "tests" in path.parts or path.name == "parsers.py":
            continue
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.ImportFrom) and node.module == "rest_framework.parsers":
                if any(alias.name == "JSONParser" for alias in node.names):
                    offenders.append(str(path.relative_to(root)))
            elif isinstance(node, ast.Attribute) and node.attr == "JSONParser":
                if isinstance(node.value, ast.Name) and node.value.id == "parsers":
                    offenders.append(str(path.relative_to(root)))
    assert offenders == []
