"""The languages of the ops console (``/django-admin/``): English, Russian and Uzbek.

**Uzbek is the source language.** Every console text is written in Uzbek in the code and is the gettext
*msgid*; ``locale/en`` and ``locale/ru`` translate it (and ``locale/ru`` / ``locale/uz`` translate the English
strings of Unfold and of Django's admin). Under Uzbek nothing of ours is looked up, so the Uzbek console is
the code's own text.

**Only the console switches language.** :class:`ConsoleLocaleMiddleware` activates the viewer's language for
console requests and nothing else: the REST API, the Celery workers and the management commands keep
``LANGUAGE_CODE`` (Uzbek), so API messages, exports, audit texts and the committed OpenAPI schema do not
depend on anybody's cookie.

**Model metadata is translated without touching the models.** :func:`localize_model_metadata` (called from
``CoreConfig.ready``) makes the verbose names, help texts and choice labels of the project's models lazy, so
the admin's column headers, form labels and filters follow the active language while the models keep their
plain Uzbek literals (and their migrations: a lazy string compares equal to its Uzbek text). Such a text is
returned **verbatim** while the language is Uzbek (:func:`in_language`): no catalog is consulted, so a label
that happens to be an English interface string too ("Email", "None") is never re-translated by Django's own
Uzbek catalog, and what the API and the OpenAPI schema read from the models cannot change.

The viewer's choice lives in a cookie scoped to the console (:data:`COOKIE_NAME`); without one the console
opens in ``OPS_DEFAULT_LANGUAGE``.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from typing import Any

from django.apps import apps
from django.conf import settings
from django.db import models
from django.http import HttpRequest, HttpResponse, HttpResponseRedirect
from django.urls import reverse
from django.utils import translation
from django.utils.functional import lazy
from django.utils.http import url_has_allowed_host_and_scheme
from django.utils.translation import gettext
from django.views.decorators.http import require_POST

from glowderma.core.network import CONSOLE_PREFIX

# (code, the language's own name): the order of the switcher.
LANGUAGES: tuple[tuple[str, str], ...] = (("en", "English"), ("ru", "Русский"), ("uz", "O‘zbekcha"))
CODES: frozenset[str] = frozenset(code for code, _name in LANGUAGES)
SOURCE_LANGUAGE = "uz"
COOKIE_NAME = "gd_ops_language"
COOKIE_PATH = f"{CONSOLE_PREFIX}/"
COOKIE_MAX_AGE = 365 * 24 * 60 * 60
PROJECT_PACKAGE = "glowderma."


def default_language() -> str:
    code = str(getattr(settings, "OPS_DEFAULT_LANGUAGE", "en"))
    return code if code in CODES else "en"


def console_language(request: HttpRequest) -> str:
    """The viewer's console language: their cookie when it names one of ours, else the default."""
    code = request.COOKIES.get(COOKIE_NAME, "")
    return code if code in CODES else default_language()


def is_console_request(request: HttpRequest) -> bool:
    return request.path_info.startswith(CONSOLE_PREFIX)


class ConsoleLocaleMiddleware:
    """Activates the viewer's language for a console request and restores the previous one afterwards, so
    a language never leaks into the next request of the worker thread (the API answers in Uzbek)."""

    def __init__(self, get_response: Callable[[HttpRequest], HttpResponse]) -> None:
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponse:
        if not is_console_request(request):
            return self.get_response(request)
        code = console_language(request)
        with translation.override(code):
            request.LANGUAGE_CODE = code
            response = self.get_response(request)
            response.headers.setdefault("Content-Language", code)
            return response


def switcher_languages(request: HttpRequest) -> list[dict[str, Any]]:
    """``UNFOLD["LANGUAGES"]["navigation"]``: the entries of the user menu's language switcher."""
    return [
        {"code": code, "name": name, "name_local": name, "name_translated": name, "bidi": False}
        for code, name in LANGUAGES
    ]


@require_POST
def set_language(request: HttpRequest) -> HttpResponse:
    """``POST /django-admin/language/`` (``language``, optional ``next``): remembers the language in the
    console cookie and returns to the page the form was on. Reachable before sign-in (the login page has a
    switcher); an unknown language changes nothing. ``next`` and the referrer are followed only on this
    host, so the form is not an open redirect."""
    target = request.POST.get("next") or request.headers.get("referer") or ""
    if not url_has_allowed_host_and_scheme(
        target, allowed_hosts={request.get_host()}, require_https=request.is_secure()
    ):
        target = reverse("admin:index")
    response = HttpResponseRedirect(target)
    code = request.POST.get("language", "")
    if code in CODES:
        response.set_cookie(
            COOKIE_NAME,
            code,
            max_age=COOKIE_MAX_AGE,
            path=COOKIE_PATH,
            secure=bool(settings.SESSION_COOKIE_SECURE),
            httponly=True,
            samesite="Lax",
        )
    return response


# --- model metadata ----------------------------------------------------------------------------------------


def in_language(text: str) -> str:
    """``text`` (Uzbek) in the active language: itself under Uzbek or with translations switched off, its
    catalog translation otherwise."""
    code = translation.get_language()
    return text if code is None or code == SOURCE_LANGUAGE else gettext(text)


_in_language_lazy: Callable[[str], Any] = lazy(in_language, str)
# Every text made lazy below: what the catalogs must translate (``metadata_texts``).
_localized: set[str] = set()


def _lazy(value: Any) -> Any:
    """``value`` as a lazy translation of itself; an empty or already lazy value (Django's own field names
    of an inherited model, a second call) is returned as it is."""
    if not isinstance(value, str) or not value:
        return value
    _localized.add(value)
    return _in_language_lazy(value)


def _lazy_choices(choices: Any) -> list[tuple[Any, Any]]:
    """``choices`` (flat or grouped) with lazy labels."""
    localized: list[tuple[Any, Any]] = []
    for value, label in choices:
        if isinstance(label, (list, tuple)):
            localized.append((_lazy(value), _lazy_choices(label)))
        else:
            localized.append((value, _lazy(label)))
    return localized


def project_models() -> Iterator[type[models.Model]]:
    for model in apps.get_models():
        if model.__module__.startswith(PROJECT_PACKAGE):
            yield model


def localize_model_metadata() -> None:
    """Makes the admin-facing metadata of the project's models and apps lazy translations of their Uzbek
    text: app names, model names, field names, help texts and choice labels. Idempotent."""
    for config in apps.get_app_configs():
        if config.name.startswith(PROJECT_PACKAGE):
            config.verbose_name = _lazy(config.verbose_name)
    for model in project_models():
        options = model._meta
        options.verbose_name = _lazy(options.verbose_name)
        options.verbose_name_plural = _lazy(options.verbose_name_plural)
        for field in options.get_fields():
            if not isinstance(field, models.Field) or field.model is not model:
                continue
            field.verbose_name = _lazy(field.verbose_name)
            field.help_text = _lazy(field.help_text)
            if field.choices:
                field.choices = _lazy_choices(field.choices)


def metadata_texts() -> set[str]:
    """The Uzbek text of every piece of metadata :func:`localize_model_metadata` made translatable."""
    return set(_localized)
