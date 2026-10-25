import csv
import re
from collections.abc import Iterable, Sequence
from datetime import datetime
from io import BytesIO, StringIO
from itertools import chain
from typing import Any, cast

from django.http import HttpResponse
from openpyxl import Workbook  # type: ignore[import-untyped]
from openpyxl.cell import WriteOnlyCell  # type: ignore[import-untyped]
from openpyxl.cell.cell import ILLEGAL_CHARACTERS_RE  # type: ignore[import-untyped]
from rest_framework.exceptions import ErrorDetail, Throttled, ValidationError
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.throttling import SimpleRateThrottle

from glowderma.core import audit, timeutils
from glowderma.core.throttling import rate_for

# A phone in E.164 (``+`` then digits only) cannot carry a formula, and contract 1.8 wants phones as plain
# E.164 text: it is the one string exempt from the leading apostrophe.
_E164 = re.compile(r"\+[1-9]\d{6,14}")


def safe_cell(value: Any) -> Any:
    if isinstance(value, str) and value.startswith(("=", "+", "-", "@", "\t", "\r")):
        if _E164.fullmatch(value):
            return value
        return "'" + value
    return value


def render_csv(rows: Iterable[Sequence[Any]], headers: Sequence[str]) -> bytes:
    stream = StringIO(newline="")
    writer = csv.writer(stream, delimiter=";", lineterminator="\r\n", quoting=csv.QUOTE_ALL)
    writer.writerow([safe_cell(value) for value in headers])
    for row in rows:
        writer.writerow([safe_cell(value) for value in row])
    return stream.getvalue().encode("utf-8-sig")


def render_xlsx(rows: Iterable[Sequence[Any]], headers: Sequence[str], sheet: str) -> bytes:
    workbook = Workbook(write_only=True)
    worksheet = workbook.create_sheet(title=sheet)
    for row in chain((headers,), rows):
        cells = []
        for raw_value in row:
            value = safe_cell(raw_value)
            if isinstance(value, str):
                cell = WriteOnlyCell(worksheet, value=ILLEGAL_CHARACTERS_RE.sub("", value))
                cell.data_type = "s"
                cells.append(cell)
            else:
                cells.append(value)
        worksheet.append(cells)
    stream = BytesIO()
    workbook.save(stream)
    workbook.close()
    return stream.getvalue()


class ExportRateThrottle(SimpleRateThrottle):
    scope = "export"

    def get_rate(self) -> str:
        return rate_for(self.scope)

    def get_cache_key(self, request: Request, view: Any) -> str:
        return self.cache_format % {"scope": self.scope, "ident": str(request.user.pk)}


_EXPORT_NON_FILTER_PARAMS = frozenset({"page", "page_size", "export"})


class ExportMixin:
    export_name: str
    export_resource: str
    export_headers: Sequence[str]
    # The query parameters the view understands; only these reach the ``export.downloaded`` audit filters.
    # ``None`` keeps every parameter but paging and ``export`` (the views that predate the allow-list).
    export_filter_keys: frozenset[str] | None = None

    def get_export_filters(self, request: Request) -> dict[str, str]:
        """The filters recorded on the ``export.downloaded`` audit row: the view's known parameters, with NUL
        removed from keys and values (JSONB cannot store ``\\u0000``; an unvalidated parameter must not turn
        a download into a 500)."""
        allowed = self.export_filter_keys
        return {
            key.replace("\x00", ""): str(value).replace("\x00", "")
            for key, value in request.query_params.items()
            if key not in _EXPORT_NON_FILTER_PARAMS and (allowed is None or key in allowed)
        }

    def get_export_rows(self, items: Iterable[Any]) -> Iterable[Sequence[Any]]:
        raise NotImplementedError("Declare the Uzbek export columns for this resource")

    def list(self: Any, request: Request, *args: Any, **kwargs: Any) -> Response:
        fmt = request.query_params.get("export")
        if fmt is None:
            return cast(Response, super().list(request, *args, **kwargs))  # type: ignore[misc]
        if fmt not in {"csv", "xlsx"}:
            raise ValidationError({"export": [ErrorDetail("Ruxsat etilmagan qiymat.", "invalid_choice")]})
        throttle = ExportRateThrottle()
        if not throttle.allow_request(request, self):
            raise Throttled(throttle.wait())
        items = list(self.filter_queryset(self.get_queryset())[:50_001])
        if len(items) > 50_000:
            raise ValidationError(
                {"export": [ErrorDetail("Juda ko‘p qator. Filtrni toraytiring.", "export_too_large")]}
            )
        rows = self.get_export_rows(items)
        if fmt == "csv":
            content = render_csv(rows, self.export_headers)
            content_type = "text/csv; charset=utf-8"
        else:
            content = render_xlsx(rows, self.export_headers, self.export_name)
            content_type = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        response = HttpResponse(content, content_type=content_type)
        stamp = timeutils.now().astimezone(timeutils.TASHKENT).strftime("%Y%m%d-%H%M")
        response["Content-Disposition"] = f'attachment; filename="{self.export_name}-{stamp}.{fmt}"'
        audit.record(
            action="export.downloaded",
            entity_type=self.export_resource,
            entity_id="",
            actor=request.user,
            request=request,
            after={
                "resource": self.export_resource,
                "filters": self.get_export_filters(request),
                "row_count": len(items),
                "format": fmt,
            },
        )
        # DRF accepts HttpResponse at dispatch; its list-mixin stubs require Response.
        return cast(Response, response)


def format_export_datetime(value: datetime | None) -> str:
    """Contract 1.8: export datetimes are text ``YYYY-MM-DD HH:MM`` in Tashkent time; ``""`` for ``None``."""
    if value is None:
        return ""
    return value.astimezone(timeutils.TASHKENT).strftime("%Y-%m-%d %H:%M")
