import csv
import re
from collections.abc import Iterable, Sequence
from datetime import datetime
from io import BytesIO, StringIO
from itertools import chain
from typing import Any, cast
from urllib.parse import quote

from django.http import HttpResponse
from django.utils import translation
from openpyxl import Workbook  # type: ignore[import-untyped]
from openpyxl.cell import WriteOnlyCell  # type: ignore[import-untyped]
from openpyxl.cell.cell import ILLEGAL_CHARACTERS_RE  # type: ignore[import-untyped]
from rest_framework.exceptions import ErrorDetail, Throttled, ValidationError
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.throttling import SimpleRateThrottle

from glowderma.core import audit, timeutils
from glowderma.core.api.language import admin_language
from glowderma.core.throttling import rate_for
from glowderma.core.uzsource import N_, rendering_context, tr

# A phone in E.164 (``+`` then digits only) cannot carry a formula, and contract 1.8 wants phones as plain
# E.164 text: it is the one string exempt from the leading apostrophe.
_E164 = re.compile(r"\+[1-9]\d{6,14}")
# What a worksheet title cannot hold (Excel): ``\ / ? * [ ] :``; it is at most 31 characters long.
_SHEET_FORBIDDEN = re.compile(r"[\\/?*\[\]:]")
SHEET_TITLE_MAX = 31
# A file name stem keeps letters, digits, ``-`` and ``_`` (any script); anything else becomes ``-``.
_FILENAME_UNSAFE = re.compile(r"[^\w-]+")
# The ASCII spelling of a Russian file name (the ``filename`` fallback next to ``filename*``): the common
# Russian-to-Latin rule (``товары`` → ``tovary``, ``журнал-действий`` → ``zhurnal-deystviy``), as the admin
# panel spells its own Russian file names.
_RUSSIAN_LATIN: dict[str, str] = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e", "ж": "zh", "з": "z", "и": "i",
    "й": "y", "к": "k", "л": "l", "м": "m", "н": "n", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t",
    "у": "u", "ф": "f", "х": "kh", "ц": "ts", "ч": "ch", "ш": "sh", "щ": "shch", "ъ": "", "ы": "y", "ь": "",
    "э": "e", "ю": "yu", "я": "ya",
}  # fmt: skip
_TO_LATIN = str.maketrans(
    {**_RUSSIAN_LATIN, **{letter.upper(): latin.capitalize() for letter, latin in _RUSSIAN_LATIN.items()}}
)


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


def sheet_title(text: str, fallback: str) -> str:
    """``text`` as a worksheet title: without the characters Excel refuses, at most 31 characters, else
    ``fallback``."""
    cleaned = _SHEET_FORBIDDEN.sub("", text).strip()[:SHEET_TITLE_MAX]
    return cleaned or fallback[:SHEET_TITLE_MAX]


def file_stem(text: str, fallback: str) -> str:
    """``text`` as a download file name stem (``товары``, ``products``), else ``fallback``."""
    cleaned = _FILENAME_UNSAFE.sub("-", text.strip()).strip("-")
    return cleaned or fallback


def ascii_stem(text: str, fallback: str) -> str:
    """``text`` (a :func:`file_stem`) spelled in ASCII — Cyrillic transliterated (``товары`` → ``tovary``) —
    for the ``filename`` fallback of :func:`attachment_header`; ``fallback`` when a character has no ASCII
    spelling."""
    latin = file_stem(text.translate(_TO_LATIN), "")
    return latin if latin and latin.isascii() else fallback


def export_context(resource: str) -> str:
    """The rendering context of the ``resource`` export (``core.uzsource.NC_`` entries such as
    ``NC_("export products", "Nomi")``)."""
    return f"export {resource}"


def attachment_header(filename: str, ascii_fallback: str) -> str:
    """``Content-Disposition`` of a download: ``filename="…"`` for an ASCII name; RFC 6266 / 5987
    ``filename*=UTF-8''…`` with the ASCII ``ascii_fallback`` as ``filename`` otherwise (Russian names)."""
    if filename.isascii():
        return f'attachment; filename="{filename}"'
    return f"attachment; filename=\"{ascii_fallback}\"; filename*=UTF-8''{quote(filename, safe='')}"


class ExportRateThrottle(SimpleRateThrottle):
    scope = "export"

    def get_rate(self) -> str:
        return rate_for(self.scope)

    def get_cache_key(self, request: Request, view: Any) -> str:
        return self.cache_format % {"scope": self.scope, "ident": str(request.user.pk)}


_EXPORT_NON_FILTER_PARAMS = frozenset({"page", "page_size", "export"})


class ExportMixin:
    """``?export=csv|xlsx`` of an admin list (contract 1.8).

    ``export_name`` (the file name stem and the sheet title) and ``export_headers`` are Uzbek source texts:
    they are rendered with ``core.uzsource.tr`` in the request's ``Accept-Language``
    (``core.api.language.admin_language``), and ``get_export_rows`` runs with that language active, so the
    values it takes from ``core.labels.text`` (and ``yes_no``, ``active_label``, ``refund_state_label``)
    follow it. Data (names, ids, amounts, dates) is never translated; the ``export.downloaded`` audit row is
    written in Uzbek outside that scope."""

    # (Not in the docstring above: drf-spectacular shows it as the description of the list operations.)
    # Everything renders under the context ``export_context(export_resource)``: a text marked
    # ``NC_("export <resource>", …)`` (``core.labels``) takes that list's own rendering. A Russian file name
    # goes out as ``filename*`` with its transliteration (``ascii_stem``) as the ``filename`` fallback.
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
            raise ValidationError({"export": [ErrorDetail(N_("Ruxsat etilmagan qiymat."), "invalid_choice")]})
        throttle = ExportRateThrottle()
        if not throttle.allow_request(request, self):
            raise Throttled(throttle.wait())
        items = list(self.filter_queryset(self.get_queryset())[:50_001])
        if len(items) > 50_000:
            raise ValidationError(
                {"export": [ErrorDetail(N_("Juda ko‘p qator. Filtrni toraytiring."), "export_too_large")]}
            )
        lang = admin_language(request)
        with translation.override(lang), rendering_context(export_context(self.export_resource)):
            headers = [tr(header) for header in self.export_headers]
            name = tr(self.export_name)
            rows = self.get_export_rows(items)
            if fmt == "csv":
                content = render_csv(rows, headers)
                content_type = "text/csv; charset=utf-8"
            else:
                content = render_xlsx(rows, headers, sheet_title(name, self.export_name))
                content_type = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        response = HttpResponse(content, content_type=content_type)
        stamp = timeutils.now().astimezone(timeutils.TASHKENT).strftime("%Y%m%d-%H%M")
        stem = file_stem(name, self.export_name)
        response["Content-Disposition"] = attachment_header(
            f"{stem}-{stamp}.{fmt}", f"{ascii_stem(stem, self.export_name)}-{stamp}.{fmt}"
        )
        response["Content-Language"] = lang
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
