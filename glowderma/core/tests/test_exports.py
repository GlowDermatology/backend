import csv
from collections.abc import Iterable, Sequence
from io import BytesIO, StringIO
from time import perf_counter
from typing import Any, cast
from unittest.mock import patch
from zipfile import ZipFile

import pytest
from django.contrib.auth import get_user_model
from django.db.models import QuerySet
from openpyxl import load_workbook  # type: ignore[import-untyped]
from openpyxl.worksheet.worksheet import Worksheet  # type: ignore[import-untyped]
from rest_framework import serializers
from rest_framework.generics import ListAPIView
from rest_framework.permissions import AllowAny
from rest_framework.test import APIRequestFactory, force_authenticate

from glowderma.core.exports import ExportMixin, render_csv, render_xlsx, safe_cell
from glowderma.core.models import AuditEntry


def test_csv_formula_injection_neutralized() -> None:
    value = '=HYPERLINK("http://x","y")'
    parsed = list(
        csv.reader(StringIO(render_csv([[value, -3]], ["name", "amount"]).decode("utf-8-sig")), delimiter=";")
    )
    assert parsed[1] == ["'" + value, "-3"]
    for prefix in ("=", "+", "-", "@", "\t", "\r"):
        assert safe_cell(prefix + "value") == "'" + prefix + "value"


def test_xlsx_cells_are_text() -> None:
    workbook = load_workbook(
        BytesIO(render_xlsx([["=1+1", "+998901234567", 100]], ["name", "phone", "amount"], "test"))
    )
    sheet = workbook.active
    assert sheet["A2"].data_type == sheet["B2"].data_type == "s"
    assert sheet["A2"].value == "'=1+1"
    assert sheet["C2"].data_type == "n"
    workbook.close()


def test_csv_has_bom_semicolons_and_quotes() -> None:
    content = render_csv([["a", 7]], ["name", "amount"])
    assert content == b'\xef\xbb\xbf"name";"amount"\r\n"a";"7"\r\n'


def test_xlsx_streams_fifty_thousand_rows_without_random_access() -> None:
    with patch.object(Worksheet, "__getitem__", side_effect=AssertionError("quadratic row lookup")):
        started = perf_counter()
        content = render_xlsx((("Customer", index) for index in range(50_000)), ["name", "amount"], "test")
        elapsed = perf_counter() - started
    assert elapsed < 8, f"50,000-row export took {elapsed:.2f}s"
    with ZipFile(BytesIO(content)) as archive:
        assert archive.read("xl/worksheets/sheet1.xml").count(b"<row ") == 50_001


def test_xlsx_removes_illegal_characters_without_logging_values(caplog: pytest.LogCaptureFixture) -> None:
    content = render_xlsx([["=private\x00", "hello\x07world", "\x01=1+1", 42]], ["a", "b", "c", "d"], "test")
    workbook = load_workbook(BytesIO(content))
    sheet = workbook.active
    assert [cell.value for cell in sheet[2]] == ["'=private", "helloworld", "=1+1", 42]
    assert [cell.data_type for cell in sheet[2]] == ["s", "s", "s", "n"]
    workbook.close()
    assert "private" not in caplog.text


class AuditSerializer(serializers.ModelSerializer[AuditEntry]):
    class Meta:
        model = AuditEntry
        fields = ["action", "entity_id"]


class ExportView(ExportMixin, ListAPIView[AuditEntry]):
    permission_classes = [AllowAny]
    serializer_class = AuditSerializer
    export_name = "audit"
    export_resource = "audit-log"
    export_headers = ["action", "entity_id"]

    def get_queryset(self) -> QuerySet[AuditEntry]:
        return AuditEntry.objects.order_by("id")

    def get_export_rows(self, items: Iterable[AuditEntry]) -> Iterable[Sequence[Any]]:
        return ((item.action, item.entity_id) for item in items)


@pytest.mark.django_db
def test_export_writes_downloaded_audit() -> None:
    user = get_user_model().objects.create_user("admin@x.uz")
    AuditEntry.objects.create(actor_type="system", action="test", entity_type="test", entity_id="1")
    request = APIRequestFactory().get(
        "/api/test/", {"export": "csv", "page": "99", "page_size": "1", "search": "test"}
    )
    force_authenticate(request, user=user)
    response = ExportView.as_view()(request)
    assert response.status_code == 200
    assert response["Content-Disposition"].startswith('attachment; filename="audit-')
    entry = AuditEntry.objects.get(action="export.downloaded")
    assert entry.actor_id == user.pk
    assert entry.after == {
        "resource": "audit-log",
        "filters": {"search": "test"},
        "row_count": 1,
        "format": "csv",
    }


@pytest.mark.django_db
def test_export_too_large_is_field_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ExportView, "get_queryset", lambda self: [None] * 50_001)
    request = APIRequestFactory().get("/api/test/", {"export": "csv"})
    force_authenticate(request, user=get_user_model().objects.create_user("admin@x.uz"))
    response = ExportView.as_view()(request)
    assert response.status_code == 400
    assert cast(Any, response).data["errors"] == [
        {"field": "export", "code": "export_too_large", "message": "Juda ko‘p qator. Filtrni toraytiring."}
    ]


@pytest.mark.django_db
def test_export_throttle_and_normal_list() -> None:
    user = get_user_model().objects.create_user("admin@x.uz")
    factory = APIRequestFactory()
    request = factory.get("/api/test/")
    force_authenticate(request, user=user)
    assert ExportView.as_view()(request).status_code == 200
    for _ in range(10):
        request = factory.get("/api/test/", {"export": "csv"})
        force_authenticate(request, user=user)
        assert ExportView.as_view()(request).status_code == 200
    request = factory.get("/api/test/", {"export": "csv"})
    force_authenticate(request, user=user)
    assert ExportView.as_view()(request).status_code == 429
