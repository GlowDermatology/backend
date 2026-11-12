"""The message catalogs of the ops console's languages (:mod:`glowderma.core.console_i18n`).

GNU gettext is in neither the dev container nor the image, so ``makemessages`` is not available; this module
collects the console's messages itself and keeps ``locale/en`` and ``locale/ru`` in step with the code
(``manage.py console_messages``; the ``.mo`` files come from ``manage.py compile_locale``).

A console message is an **Uzbek** msgid from one of four places:

* a marked literal in Python — the first argument of ``_`` / ``gettext`` / ``gettext_lazy`` /
  ``gettext_noop`` / ``ngettext`` / ``pgettext`` … (:func:`python_messages`, by ``ast``);
* a ``{% translate %}`` / ``{% blocktranslate %}`` or a ``_("…")`` argument of a template
  (:func:`template_messages`, through Django's own ``templatize``);
* model metadata made lazy at start-up (``console_i18n.metadata_texts``);
* a label the console translates when it renders it: the mappings of :mod:`glowderma.core.labels`
  (:func:`dynamic_texts`) and the field names Django's admin history stores (:func:`history_texts`).

``locale/en`` translates exactly these. ``locale/ru`` translates these **and** every English string of Unfold
and the Django admin that ``locale/uz`` translates (:func:`foreign_messages`): Django falls back from Russian
to ``LANGUAGE_CODE``, so a string missing there would show in Uzbek.
"""

from __future__ import annotations

import ast
import re
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from string import Formatter
from typing import Final

from django.conf import settings
from django.db import models
from django.utils import translation
from django.utils.text import capfirst
from django.utils.translation.template import templatize

from glowderma.core import console_i18n, labels
from glowderma.core.po import Entry, parse

DOMAIN: Final = "django"
SOURCE_LANGUAGE: Final = "uz"
TARGET_LANGUAGES: Final[tuple[str, ...]] = ("en", "ru")
# msgstr[n] per language (the Plural-Forms of the catalog headers below).
PLURAL_FORMS: Final[dict[str, tuple[int, str]]] = {
    "en": (2, "nplurals=2; plural=(n != 1);"),
    "ru": (
        4,
        "nplurals=4; plural=(n % 10 == 1 && n % 100 != 11 ? 0 : n % 10 >= 2 && n % 10 <= 4 && "
        "(n % 100 < 12 || n % 100 > 14) ? 1 : n % 10 == 0 || (n % 10 >= 5 && n % 10 <= 9) || "
        "(n % 100 >= 11 && n % 100 <= 14) ? 2 : 3);",
    ),
}
SINGULAR: Final = frozenset({"_", "gettext", "gettext_lazy", "gettext_noop"})
PLURAL: Final = frozenset({"ngettext", "ngettext_lazy"})
CONTEXT: Final = frozenset({"pgettext", "pgettext_lazy"})
CONTEXT_PLURAL: Final = frozenset({"npgettext", "npgettext_lazy"})
SKIPPED_PARTS: Final = frozenset({"tests", "migrations", "__pycache__"})
# Python outside the package that carries console text.
EXTRA_PYTHON: Final[tuple[str, ...]] = ("config/settings/ops_admin.py",)
# ``core.labels`` names that never reach a console page (the exports' column headers stay Uzbek).
UNTRANSLATED_LABELS: Final = re.compile(r"_EXPORT_HEADERS$")
_TEMPLATE_CALL: Final = re.compile(
    r"(?<![\w.])(?P<name>n?p?gettext|_)\(\s*(?P<args>(?:u?(?:'(?:[^'\\]|\\.)*'|\"(?:[^\"\\]|\\.)*\")\s*,?\s*)+)"
)
_STRING: Final = re.compile(r"u?('(?:[^'\\]|\\.)*'|\"(?:[^\"\\]|\\.)*\")")


@dataclass(frozen=True)
class Message:
    msgid: str
    msgid_plural: str | None = None
    msgctxt: str | None = None

    def key(self) -> tuple[str | None, str]:
        return (self.msgctxt, self.msgid)


@dataclass
class Catalog:
    """What the code needs translated: each message with the places it comes from.

    gettext knows a message by its context and ``msgid`` alone, so a text used both on its own and as the
    singular of a plural is one (plural) entry, whose first form also answers the plain lookup."""

    sources: dict[Message, set[str]] = field(default_factory=dict)
    _by_key: dict[tuple[str | None, str], Message] = field(default_factory=dict)

    def add(self, message: Message, source: str) -> None:
        if not message.msgid:
            return
        known = self._by_key.get(message.key())
        if known is None:
            self._by_key[message.key()] = message
            self.sources[message] = {source}
        elif known.msgid_plural is None and message.msgid_plural is not None:
            self._by_key[message.key()] = message
            self.sources[message] = self.sources.pop(known) | {source}
        else:
            self.sources[known].add(source)

    def messages(self) -> list[Message]:
        return sorted(self.sources, key=lambda m: (m.msgid, m.msgctxt or ""))


def base_dir() -> Path:
    return Path(settings.BASE_DIR)


def _call_name(node: ast.Call) -> str | None:
    function = node.func
    if isinstance(function, ast.Name):
        return function.id
    if isinstance(function, ast.Attribute):
        return function.attr
    return None


def _text(node: ast.expr | None) -> str | None:
    return node.value if isinstance(node, ast.Constant) and isinstance(node.value, str) else None


def _message(name: str, arguments: list[str | None]) -> Message | None:
    """The message of a marker call from its leading string arguments (``None``: a dynamic argument)."""

    def at(index: int) -> str | None:
        return arguments[index] if index < len(arguments) else None

    if name in SINGULAR and at(0):
        return Message(str(at(0)))
    if name in PLURAL and at(0) and at(1):
        return Message(str(at(0)), at(1))
    if name in CONTEXT and at(0) and at(1):
        return Message(str(at(1)), None, at(0))
    if name in CONTEXT_PLURAL and at(0) and at(1) and at(2):
        return Message(str(at(1)), at(2), at(0))
    return None


def python_messages(source: str) -> Iterator[Message]:
    """The marked literals of a Python source."""
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Call):
            name = _call_name(node)
            if name is not None:
                message = _message(name, [_text(argument) for argument in node.args[:3]])
                if message is not None:
                    yield message


def template_messages(source: str) -> Iterator[Message]:
    """The ``translate`` / ``blocktranslate`` messages of a template (``templatize`` turns them into the
    ``gettext('…')`` calls ``xgettext`` reads). A literal per cent sign stays doubled (``%%``): that is the
    msgid the template tags look up."""
    for match in _TEMPLATE_CALL.finditer(templatize(source)):
        arguments: list[str | None] = [
            str(ast.literal_eval(token)) for token in _STRING.findall(match["args"])
        ]
        message = _message(match["name"], arguments)
        if message is not None:
            yield message


def python_files() -> Iterator[Path]:
    root = base_dir()
    for path in sorted((root / "glowderma").rglob("*.py")):
        if not SKIPPED_PARTS.intersection(path.parts):
            yield path
    for extra in EXTRA_PYTHON:
        yield root / extra


def template_files() -> Iterator[Path]:
    for path in sorted((base_dir() / "glowderma").rglob("templates/**/*.html")):
        if not SKIPPED_PARTS.intersection(path.parts):
            yield path


def dynamic_texts() -> dict[str, str]:
    """Labels the console translates at render time (``gettext(value)``), each with where it is defined:
    the string values of :mod:`glowderma.core.labels`. An enum's labels are not swept up: the ones a model
    field uses arrive as its choices (``metadata_texts``), and the rest are API vocabulary no page shows —
    some spell like an interface string ("All", "Order") and would take its catalog entry."""
    found: dict[str, str] = {}
    for name, value in vars(labels).items():
        if name.startswith("_") or UNTRANSLATED_LABELS.search(name):
            continue
        source = f"{labels.__name__}.{name}"
        if isinstance(value, dict):
            texts: Iterable[object] = value.values()
        elif isinstance(value, tuple):
            texts = value
        elif isinstance(value, str) and name.isupper():
            texts = (value,)
        else:
            continue
        for text in texts:
            if isinstance(text, str) and text:
                found.setdefault(text, source)
    return found


def history_texts() -> dict[str, str]:
    """Field names as Django's admin history stores them. A change of an editable page is logged with the
    form labels of the changed fields (the capitalised verbose names, in the source language) and shown
    through ``gettext``: the catalogs carry those spellings for the models an editable admin writes."""
    from django.contrib import admin

    from glowderma.core.admin import ReadOnlyAdmin

    found: dict[str, str] = {}
    for model, model_admin in admin.site._registry.items():
        if not model.__module__.startswith(console_i18n.PROJECT_PACKAGE) or isinstance(
            model_admin, ReadOnlyAdmin
        ):
            continue
        for edited in (model, *(inline.model for inline in model_admin.inlines)):
            for column in edited._meta.get_fields():
                if isinstance(column, models.Field) and column.editable and not column.auto_created:
                    with translation.override(None):
                        label = capfirst(str(column.verbose_name))
                    if label:
                        found.setdefault(label, f"admin history ({edited._meta.label})")
    return found


def collect() -> Catalog:
    """Every message the console needs in English and Russian."""
    catalog = Catalog()
    root = base_dir()
    for path in python_files():
        for message in python_messages(path.read_text(encoding="utf-8")):
            catalog.add(message, str(path.relative_to(root)))
    for path in template_files():
        for message in template_messages(path.read_text(encoding="utf-8")):
            catalog.add(message, str(path.relative_to(root)))
    for text in sorted(console_i18n.metadata_texts()):
        catalog.add(Message(text), "model metadata")
    for text, source in dynamic_texts().items():
        catalog.add(Message(text), source)
    for text, source in history_texts().items():
        catalog.add(Message(text), source)
    return catalog


def catalog_path(language: str, domain: str = DOMAIN) -> Path:
    return base_dir() / "locale" / language / "LC_MESSAGES" / f"{domain}.po"


def read_entries(language: str, domain: str = DOMAIN) -> list[Entry]:
    path = catalog_path(language, domain)
    return (
        [entry for entry in parse(path.read_text(encoding="utf-8")) if entry.msgid] if path.exists() else []
    )


def foreign_messages(domain: str = DOMAIN) -> list[Message]:
    """The English strings of Unfold and the Django admin that the Uzbek catalog translates: Russian needs
    them too."""
    return [Message(entry.msgid, entry.msgid_plural, entry.msgctxt) for entry in read_entries("uz", domain)]


def required(language: str, catalog: Catalog) -> list[Message]:
    """What ``language`` must translate; an interface string the console's own code also uses is one entry."""
    messages = catalog.messages()
    if language != "ru":
        return messages
    known = {message.key() for message in messages}
    return [*messages, *(message for message in foreign_messages() if message.key() not in known)]


def placeholders(text: str) -> set[str]:
    """The ``%(name)s`` and ``{name}`` placeholders of a message."""
    named = set(re.findall(r"%\((\w+)\)[a-zA-Z]", text))
    try:
        braces = {name for _literal, name, _spec, _conv in Formatter().parse(text) if name}
    except ValueError:
        braces = set()
    return named | braces


def problems(language: str, catalog: Catalog) -> list[str]:
    """Why ``locale/<language>`` is not complete and exact for ``catalog``; empty when it is."""
    forms = PLURAL_FORMS[language][0]
    entries = {(entry.msgctxt, entry.msgid): entry for entry in read_entries(language)}
    wanted = required(language, catalog)
    found: list[str] = []
    for message in wanted:
        entry = entries.get(message.key())
        if entry is None:
            found.append(f"missing: {message.msgid!r}")
            continue
        if entry.msgid_plural != message.msgid_plural:
            found.append(f"plural mismatch: {message.msgid!r}")
            continue
        expected = range(forms) if message.msgid_plural is not None else range(1)
        texts = [entry.msgstr.get(index, "") for index in expected]
        if not all(texts):
            found.append(f"untranslated: {message.msgid!r}")
            continue
        names = placeholders(message.msgid) | placeholders(message.msgid_plural or "")
        for text in texts:
            # A plural form may leave the count out ("One result"); it never invents a placeholder.
            extra = placeholders(text) - names
            lost = names - placeholders(text) if message.msgid_plural is None else set()
            if extra or lost:
                found.append(f"placeholders differ: {message.msgid!r} -> {text!r}")
    keys = {message.key() for message in wanted}
    found.extend(f"obsolete: {entry.msgid!r}" for key, entry in entries.items() if key not in keys)
    return found


def _quote(text: str) -> str:
    escaped = text.replace("\\", "\\\\").replace('"', '\\"').replace("\t", "\\t").replace("\n", "\\n")
    return f'"{escaped}"'


def render(language: str, catalog: Catalog, translations: dict[tuple[str | None, str], Entry]) -> str:
    """The ``.po`` text of ``language``: every required message in a stable order, with the translation
    ``translations`` has for it (an empty ``msgstr`` otherwise)."""
    forms, rule = PLURAL_FORMS[language]
    lines = [
        f"# Glow Derma ops console, {language}: kept by `manage.py console_messages`, translated by hand.",
        "# Compile with `manage.py compile_locale` (stdlib, no GNU gettext); commit the .mo with it.",
        'msgid ""',
        'msgstr ""',
        '"Content-Type: text/plain; charset=UTF-8\\n"',
        f'"Language: {language}\\n"',
        f'"Plural-Forms: {rule}\\n"',
    ]
    for message in required(language, catalog):
        entry = translations.get(message.key())
        lines.append("")
        lines.extend(
            f"#: {source}"
            for source in sorted(catalog.sources.get(message, ("unfold / django.contrib.admin",)))
        )
        if message.msgctxt is not None:
            lines.append(f"msgctxt {_quote(message.msgctxt)}")
        lines.append(f"msgid {_quote(message.msgid)}")
        if message.msgid_plural is None:
            lines.append(f"msgstr {_quote(entry.msgstr.get(0, '') if entry else '')}")
        else:
            lines.append(f"msgid_plural {_quote(message.msgid_plural)}")
            lines.extend(
                f"msgstr[{index}] {_quote(entry.msgstr.get(index, '') if entry else '')}"
                for index in range(forms)
            )
    return "\n".join(lines) + "\n"


def sync(language: str, catalog: Catalog) -> str:
    """Rewrites ``locale/<language>/LC_MESSAGES/django.po`` for ``catalog``, keeping the translations it has;
    returns the new text."""
    translations = {(entry.msgctxt, entry.msgid): entry for entry in read_entries(language)}
    text = render(language, catalog, translations)
    path = catalog_path(language)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return text
