"""The admin API's response language (feat/admin-ux, DESIGN §G).

The admin panel sends its language in ``Accept-Language`` (``uz``, ``ru`` or ``en``); the admin API answers
error messages and exports in it and says so in ``Content-Language``. Nothing else depends on it: the request
runs in ``LANGUAGE_CODE`` (Uzbek), and what it stores or sends stays Uzbek (``core.uzsource``).
"""

from __future__ import annotations

from typing import Any, Final

from glowderma.core import i18n

ADMIN_PREFIX: Final = "/api/v1/admin"


def is_admin_path(path: str) -> bool:
    """``path`` is under ``/api/v1/admin/`` (the segment itself, with or without the trailing slash)."""
    return path == ADMIN_PREFIX or path.startswith(ADMIN_PREFIX + "/")


def admin_language(request: Any) -> str:
    """The first supported language of the request's ``Accept-Language`` in q-order (``ru-RU`` → ``ru``),
    else ``uz``. Reads the header only — never the staff account or a cookie — so the same request gets the
    same answer before and after authentication."""
    return i18n.resolve_language(request)


def request_admin_language(request: Any) -> str:
    """:func:`admin_language` for a request of the admin API, ``uz`` for any other path (health, schema)."""
    path = str(getattr(request, "path_info", "") or getattr(request, "path", "") or "")
    return admin_language(request) if request is not None and is_admin_path(path) else i18n.DEFAULT_LANGUAGE
