"""Uzbek source texts of the admin API and their Russian / English renderings (feat/admin-ux, DESIGN §G).

**Uzbek is the source language and the only language anything is stored or sent in.** A staff-visible text
of the admin API (an error message, an export header) is written in Uzbek and marked with :data:`N_` — a
no-op that returns the text unchanged and lets ``manage.py console_messages`` collect it into
``locale/en`` and ``locale/ru`` with the ops console's texts (one catalog, one tooling). Interpolate with
named placeholders: ``N_("Kamida %(n)s ta.") % {"n": 3}``.

A text is put into the reader's language only at the edge, where it leaves for that reader:

* :func:`localize_admin_message` — the admin error envelope (``core.api.exceptions``) renders ``message``
  and ``errors[].message`` in the request's ``Accept-Language`` (``core.api.language.admin_language``);
* :func:`tr` — the export renderer (``core.exports.ExportMixin``) renders headers, the sheet title and the
  file name, and ``core.labels.text`` the values, in the active language, which the renderer switches to
  the request's language only around building the file.

A text that reads differently in one place than the same Uzbek word elsewhere ("Nomi" is a person's name in
the ops console and a thing's name in the product export; Russian adjectives agree with their noun) is marked
once more with its context, :func:`NC_` (``NC_("export products", "Nomi")``); :func:`tr` looks it up under
the context of :func:`rendering_context` (the export renderer: ``export <resource>``) before the plain
message.

Nothing else calls these: audit rows, outbox payloads, notifications, order events and e-mails keep the
Uzbek text whatever the request's language (the canary test ``core/tests/test_admin_api_languages.py``).

The translations come from the project's compiled catalogs (``LOCALE_PATHS/<lang>/LC_MESSAGES/django.mo``),
limited to the project's own messages: the msgids of ``locale/en`` (``locale/ru`` also holds the English
strings of Unfold and Django, which are never an Uzbek source text).
"""

from __future__ import annotations

import gettext as gettext_module
import re
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Final

from django.conf import settings
from django.utils import translation
from django.utils.translation import gettext_noop

SOURCE_LANGUAGE: Final = "uz"
TARGET_LANGUAGES: Final[tuple[str, ...]] = ("en", "ru")
NBSP: Final = "\u00a0"
MINUS: Final = "\u2212"
# Reverse matching reads a value back out of a rendered text: a text longer than this (never a real message)
# is only looked up exactly, so a long user value echoed in a message cannot make the matching slow.
MAX_MATCHED_LENGTH: Final = 2000
# A template needs this many letters outside its placeholders to be matched in reverse: "%(a)s: %(b)s"
# would match anything.
MIN_TEMPLATE_LETTERS: Final = 3

N_ = gettext_noop
"""Marks an Uzbek source text of the admin API (returns it unchanged; ``console_messages`` collects it)."""

# The context of the rendering under way (:func:`rendering_context`); ``None`` outside one.
_RENDERING_CONTEXT: ContextVar[str | None] = ContextVar("uzsource_rendering_context", default=None)
# gettext's separator of a context and its msgid in a compiled catalog
_CONTEXT_SEPARATOR: Final = "\x04"


def NC_(context: str, text: str) -> str:  # noqa: N802 - named like N_, the marker it extends
    """Marks the Uzbek ``text`` with a ``context`` (returns ``text``; ``console_messages`` collects it as a
    ``msgctxt`` entry): its rendering under :func:`rendering_context` ``context``."""
    del context
    return text


@contextmanager
def rendering_context(context: str | None) -> Iterator[None]:
    """Render with :func:`tr` under ``context`` (a :func:`NC_` entry first, the plain message otherwise)."""
    token = _RENDERING_CONTEXT.set(context)
    try:
        yield
    finally:
        _RENDERING_CONTEXT.reset(token)


# ``%(name)s`` (any conversion letter) or ``{name}`` placeholders, and the escapes of each style.
_TOKEN: Final = re.compile(r"%\((?P<pct>\w+)\)[a-zA-Z]|\{(?P<brace>\w+)\}|%%|\{\{|\}\}")
_ESCAPES: Final = {"%%": "%", "{{": "{", "}}": "}"}
# An integer as ``core.i18n.format_number(n, "uz")`` renders it: optional U+2212 minus, NBSP-grouped digits.
_UZ_NUMBER: Final = re.compile("(\u2212?)([0-9]{1,3}(?:\u00a0[0-9]{3})*)")


def _mo_catalog(language: str, *, contextual: bool = False) -> dict[str, str]:
    """The singular entries of the project's compiled catalog of ``language`` (msgid → msgstr); with
    ``contextual`` only the entries with a context instead (``"<msgctxt>\\x04<msgid>"`` → msgstr)."""
    found: dict[str, str] = {}
    for root in reversed([Path(path) for path in getattr(settings, "LOCALE_PATHS", ())]):
        path = root / language / "LC_MESSAGES" / "django.mo"
        if not path.is_file():
            continue
        with path.open("rb") as handle:
            compiled = gettext_module.GNUTranslations(handle)
        entries: Mapping[object, object] = getattr(compiled, "_catalog", {})
        for msgid, msgstr in entries.items():
            # plural forms are keyed (msgid, n): never an admin message; a context is "ctx\x04msgid"
            if (
                isinstance(msgid, str)
                and msgid
                and (_CONTEXT_SEPARATOR in msgid) == contextual
                and isinstance(msgstr, str)
                and msgstr
            ):
                found[msgid] = msgstr
    return found


@lru_cache(maxsize=4)
def catalog(language: str) -> Mapping[str, str]:
    """The project's own messages in ``language`` (Uzbek msgid → translation); empty for Uzbek or an unknown
    language. Cached for the process: the compiled catalogs change only with a deploy."""
    if language not in TARGET_LANGUAGES:
        return {}
    own = _mo_catalog("en")
    entries = own if language == "en" else _mo_catalog(language)
    return {msgid: text for msgid, text in entries.items() if msgid in own}


@lru_cache(maxsize=4)
def context_catalog(language: str) -> Mapping[str, str]:
    """The project's own :func:`NC_` messages in ``language`` (``"<context>\\x04<Uzbek msgid>"`` →
    translation), like :func:`catalog`."""
    if language not in TARGET_LANGUAGES:
        return {}
    own = _mo_catalog("en", contextual=True)
    entries = own if language == "en" else _mo_catalog(language, contextual=True)
    return {key: text for key, text in entries.items() if key in own}


def lookup(text: str, language: str | None, context: str | None = None) -> str | None:
    """The translation of the Uzbek ``text`` in ``language`` when the catalog has it exactly — under
    ``context`` when it has that entry — else ``None``."""
    if not language or language == SOURCE_LANGUAGE:
        return None
    if context:
        found = context_catalog(language).get(f"{context}{_CONTEXT_SEPARATOR}{text}")
        if found:
            return found
    return catalog(language).get(text)


def tr(text: str, language: str | None = None) -> str:
    """``text`` (Uzbek) in ``language`` — by default the active language: itself under Uzbek, with
    translations switched off or when the catalog does not have it, its catalog translation otherwise (its
    :func:`NC_` entry first inside :func:`rendering_context`).

    The admin API runs in ``LANGUAGE_CODE`` (Uzbek), so a ``tr`` call is the identity there unless the code
    switches the language for building a response (the export renderer). Never use it for a text that is
    stored or sent."""
    if not text:
        return text
    language = translation.get_language() if language is None else language
    return lookup(text, language, _RENDERING_CONTEXT.get()) or text


# --- reverse template matching (a rendered message → its template and values) -------------------------------


@dataclass(frozen=True)
class Template:
    msgid: str
    pattern: re.Pattern[str]
    translation: str


def _tokens(template: str) -> list[tuple[bool, str]]:
    """``template`` as (is_placeholder, literal text or placeholder name) tokens."""
    tokens: list[tuple[bool, str]] = []
    position = 0
    for match in _TOKEN.finditer(template):
        if match.start() > position:
            tokens.append((False, template[position : match.start()]))
        name = match.group("pct") or match.group("brace")
        tokens.append((True, name) if name else (False, _ESCAPES[match.group(0)]))
        position = match.end()
    if position < len(template):
        tokens.append((False, template[position:]))
    return tokens


def placeholders(template: str) -> set[str]:
    """The ``%(name)s`` / ``{name}`` placeholder names of a template."""
    return {value for is_name, value in _tokens(template) if is_name}


def _pattern(template: str) -> re.Pattern[str] | None:
    """The regular expression that reads a rendering of ``template`` back (``None``: no placeholder, or too
    few letters outside them to be told apart from other texts)."""
    tokens = _tokens(template)
    if not any(is_name for is_name, _value in tokens):
        return None
    literal = "".join(value for is_name, value in tokens if not is_name)
    if sum(char.isalpha() for char in literal) < MIN_TEMPLATE_LETTERS:
        return None
    pattern = ""
    seen: set[str] = set()
    for is_name, value in tokens:
        if not is_name:
            pattern += re.escape(value)
        elif value in seen:
            pattern += f"(?P={value})"
        else:
            seen.add(value)
            pattern += f"(?P<{value}>.+?)"
    try:
        return re.compile(pattern, re.DOTALL)
    except re.error:
        return None


@lru_cache(maxsize=4)
def templates(language: str) -> tuple[Template, ...]:
    """The catalog's templates of ``language`` with their reading patterns, the most specific (longest
    literal text) first."""
    found: list[tuple[int, str, Template]] = []
    for msgid, text in catalog(language).items():
        pattern = _pattern(msgid)
        if pattern is not None and placeholders(text) <= placeholders(msgid):
            weight = len("".join(value for is_name, value in _tokens(msgid) if not is_name))
            found.append((-weight, msgid, Template(msgid, pattern, text)))
    return tuple(template for _weight, _msgid, template in sorted(found, key=lambda item: item[:2]))


def localize_value(value: str, language: str) -> str:
    """A value read out of an Uzbek message, for ``language``: an Uzbek-grouped integer (``12 000``) is
    re-grouped (``12,000`` in English, unchanged in Russian), a text the catalog has exactly (a label such
    as an order status) is translated, anything else (a name, a number, an id) is kept."""
    match = _UZ_NUMBER.fullmatch(value)
    if match is not None:
        if language != "en":
            return value
        return ("-" if match.group(1) == MINUS else "") + match.group(2).replace(NBSP, ",")
    return lookup(value, language) or value


def _render(template: str, values: Mapping[str, str]) -> str:
    return "".join(values.get(value, "") if is_name else value for is_name, value in _tokens(template))


def localize_admin_message(text: str, language: str | None) -> str:
    """An admin API message (Uzbek, as raised) in ``language``.

    1. The catalog has the text exactly → its translation.
    2. Else the first catalog template whose Uzbek form reads the text back (``Kamida 3 ta belgi kiriting.``
       is ``Kamida {limit} ta belgi kiriting.`` with ``limit=3``) → the translated template, filled with the
       values (each through :func:`localize_value`). Values are inserted as they are: a value that looks
       like a placeholder is never expanded.
    3. Else (a message nobody marked yet, a free text) the Uzbek text unchanged.

    Under Uzbek, ``None`` or an unknown language the text is returned as it is.
    """
    if not isinstance(text, str) or not text or language not in TARGET_LANGUAGES:
        return text
    exact = lookup(text, language)
    if exact is not None:
        return exact
    if len(text) > MAX_MATCHED_LENGTH:
        return text
    for template in templates(language):
        match = template.pattern.fullmatch(text)
        if match is not None:
            values = {name: localize_value(value, language) for name, value in match.groupdict().items()}
            return _render(template.translation, values)
    return text


def clear_caches() -> None:
    """Forget the loaded catalogs (tests that compile or fake a catalog)."""
    catalog.cache_clear()
    context_catalog.cache_clear()
    templates.cache_clear()
