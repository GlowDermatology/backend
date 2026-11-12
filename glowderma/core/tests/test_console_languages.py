"""The ops console's three languages (glowderma.core.console_i18n): the default, the switcher and its cookie,
the scope (console only), the lazy model metadata and the completeness of the en / ru catalogs."""

from __future__ import annotations

import ast
import re
from collections.abc import Iterator
from pathlib import Path

import pytest
from django.apps import apps
from django.conf import settings
from django.contrib import admin
from django.core.cache import cache
from django.core.management import call_command
from django.test import Client, override_settings
from django.urls import reverse
from django.utils import translation

from glowderma.commerce.models import Order
from glowderma.core import console_catalog, console_i18n
from glowderma.core.po import compile_po, duplicates, parse
from glowderma.core.tests.console import console_client

LOGIN = "/django-admin/login/"
SWITCH = "/django-admin/language/"
# Uzbek (Latin) spells o‘ and g‘ with U+2018 (or U+02BB): a page in another language has neither. The frequent
# console words catch a text that has no such letter.
UZBEK = re.compile(
    r"[OoGg][‘ʻ]|\b(holat|buyurtma|mijoz|mahsulot|yaratilgan|yangilangan|navbat|xato|sana|turi|nomi|qaytarish"
    r"|xodim|sozlama|bildirishnoma|hisobot|tasdiq|bekor|yuboril|qidir|saqlash|kirish|chiqish|jami|barcha|yangi"
    r"|oxirgi|kategoriya|xizmat|mutaxassis|qabul|manzil|izoh|sabab|narx|qoldiq)\w*",
    re.IGNORECASE,
)
CYRILLIC = re.compile(r"[А-Яа-яЁё]{3,}")
TAGS = re.compile(r"<(script|style)\b.*?</\1>|<[^>]+>", re.DOTALL)
JSON_BLOCK = re.compile(r'<pre class="gd-json">.*?</pre>', re.DOTALL)


def with_language(client: Client, code: str) -> Client:
    client.cookies[console_i18n.COOKIE_NAME] = code
    return client


def text_of(response: object) -> str:
    """The page's visible text, without its JSON documents (data) and the switcher's own entry (the Uzbek
    language is named in Uzbek)."""
    html = JSON_BLOCK.sub(" ", response.content.decode())  # type: ignore[attr-defined]
    return TAGS.sub(" ", html).replace(dict(console_i18n.LANGUAGES)["uz"], "")


# --- the default and the cookie ---------------------------------------------------------------------------


@pytest.mark.django_db
@override_settings(OPS_DEFAULT_LANGUAGE="en")
def test_the_console_opens_in_english_for_a_browser_without_a_choice() -> None:
    page = Client().get(LOGIN)
    assert page.status_code == 200
    assert '<html lang="en"' in page.content.decode()
    assert page["Content-Language"] == "en"
    assert not UZBEK.search(text_of(page))


@pytest.mark.django_db
@pytest.mark.parametrize(("code", "marker"), [("ru", "Войти"), ("uz", "Kirish"), ("en", "Log in")])
@override_settings(OPS_DEFAULT_LANGUAGE="en")
def test_the_cookie_chooses_the_language(code: str, marker: str) -> None:
    page = with_language(Client(), code).get(LOGIN)
    assert f'<html lang="{code}"' in page.content.decode()
    assert marker in text_of(page)


@pytest.mark.django_db
@pytest.mark.parametrize("value", ["de", "", "en-us", "../uz", "x" * 300])
@override_settings(OPS_DEFAULT_LANGUAGE="ru")
def test_an_unknown_cookie_value_falls_back_to_the_default(value: str) -> None:
    page = with_language(Client(), value).get(LOGIN)
    assert '<html lang="ru"' in page.content.decode()


def test_the_default_language_must_be_one_of_the_three() -> None:
    assert settings.OPS_DEFAULT_LANGUAGE in console_i18n.CODES
    assert {code for code, _name in settings.LANGUAGES} == console_i18n.CODES
    with override_settings(OPS_DEFAULT_LANGUAGE="de"):
        assert console_i18n.default_language() == "en"


# --- the switcher -----------------------------------------------------------------------------------------


@pytest.mark.django_db
def test_the_sign_in_page_offers_the_three_languages() -> None:
    html = Client().get(LOGIN).content.decode()
    assert f'action="{SWITCH}"' in html
    for code, name in console_i18n.LANGUAGES:
        assert f'name="language" value="{code}"' in html
        assert name in html
    assert html.count('aria-current="true"') == 1


@pytest.mark.django_db
def test_the_user_menu_offers_the_three_languages() -> None:
    html = console_client("staff").get(reverse("admin:index")).content.decode()
    assert f'action="{SWITCH}"' in html
    for code, name in console_i18n.LANGUAGES:
        assert f'name="language" type="hidden" value="{code}"' in html
        assert name in html


@pytest.mark.django_db
def test_choosing_a_language_sets_the_console_cookie_and_returns_to_the_page() -> None:
    client = Client()
    response = client.post(SWITCH, {"language": "ru", "next": "/django-admin/login/?next=/django-admin/"})
    assert response.status_code == 302
    assert response["Location"] == "/django-admin/login/?next=/django-admin/"
    cookie = response.cookies[console_i18n.COOKIE_NAME]
    assert cookie.value == "ru"
    assert cookie["path"] == "/django-admin/"
    assert cookie["httponly"]
    assert cookie["samesite"] == "Lax"
    assert int(cookie["max-age"]) == console_i18n.COOKIE_MAX_AGE
    assert '<html lang="ru"' in client.get(LOGIN).content.decode()


@pytest.mark.django_db
def test_the_switch_returns_to_the_referring_page_when_the_form_names_none() -> None:
    response = Client().post(
        SWITCH, {"language": "en"}, headers={"referer": "http://testserver/django-admin/2fa/"}
    )
    assert response["Location"] == "http://testserver/django-admin/2fa/"


@pytest.mark.django_db
@pytest.mark.parametrize(
    "target",
    ["https://evil.example/", "//evil.example/x", "javascript:alert(1)", "http://evil.example/django-admin/"],
)
def test_the_switch_never_redirects_off_the_console_host(target: str) -> None:
    by_form = Client().post(SWITCH, {"language": "en", "next": target})
    by_referrer = Client().post(SWITCH, {"language": "en"}, headers={"referer": target})
    assert by_form["Location"] == by_referrer["Location"] == reverse("admin:index")


@pytest.mark.django_db
def test_an_unknown_language_changes_nothing_and_a_get_is_refused() -> None:
    response = Client().post(SWITCH, {"language": "de", "next": LOGIN})
    assert response.status_code == 302
    assert console_i18n.COOKIE_NAME not in response.cookies
    assert Client().get(SWITCH).status_code == 405


@pytest.mark.django_db
def test_the_switch_is_csrf_protected() -> None:
    assert Client(enforce_csrf_checks=True).post(SWITCH, {"language": "ru"}).status_code == 403


@override_settings(SESSION_COOKIE_SECURE=True)
@pytest.mark.django_db
def test_the_language_cookie_is_secure_where_the_session_cookie_is() -> None:
    response = Client().post(SWITCH, {"language": "uz"}, secure=True)
    assert response.cookies[console_i18n.COOKIE_NAME]["secure"]


# --- only the console switches ----------------------------------------------------------------------------


@pytest.mark.django_db
def test_the_api_and_the_worker_thread_stay_uzbek() -> None:
    client = with_language(Client(), "ru")
    assert translation.get_language() == "uz"
    assert '<html lang="ru"' in client.get(LOGIN).content.decode()
    # the console's language does not outlive its request
    assert translation.get_language() == "uz"
    api = client.get("/api/v1/admin/orders/", REMOTE_ADDR="127.0.0.1")
    assert "Content-Language" not in api.headers or api["Content-Language"] != "ru"
    assert "Avtorizatsiya" in api.content.decode() or api.status_code in {401, 403}
    assert not CYRILLIC.search(api.content.decode())


@pytest.mark.django_db
def test_the_schema_is_generated_in_the_source_language(tmp_path: Path) -> None:
    """The committed OpenAPI schema carries the models' Uzbek help texts whatever language is active."""
    target = tmp_path / "schema.yaml"
    with translation.override("ru"):
        call_command("spectacular", "--file", str(target), "--lang", "uz")
    committed = Path(settings.BASE_DIR) / "openapi" / "schema.yaml"
    assert target.read_bytes() == committed.read_bytes()


# --- model metadata ---------------------------------------------------------------------------------------


def test_model_names_fields_and_choices_follow_the_language() -> None:
    options = Order._meta
    status = options.get_field("status")
    with translation.override("uz"):
        assert str(options.verbose_name) == "buyurtma"
        uzbek_choices = [str(label) for _value, label in status.flatchoices]
    with translation.override("en"):
        assert str(options.verbose_name) == "order"
        assert str(options.verbose_name_plural) == "orders"
        assert str(status.verbose_name) == "status"
        english_choices = [str(label) for _value, label in status.flatchoices]
        assert str(apps.get_app_config("commerce").verbose_name) != ""
    with translation.override("ru"):
        assert str(options.verbose_name) == "заказ"
    assert len(english_choices) == len(uzbek_choices)
    assert not set(english_choices) & set(uzbek_choices)


def test_the_models_keep_their_declared_text_for_migrations() -> None:
    """A lazy label equals its Uzbek text, so ``makemigrations --check`` sees no change in any language."""
    for code in ("uz", "en", "ru"):
        with translation.override(code):
            assert Order._meta.get_field("status").deconstruct()[3]["verbose_name"] == "holat"
    console_i18n.localize_model_metadata()  # idempotent
    with translation.override(None):
        assert str(Order._meta.verbose_name) == "buyurtma"


# --- the catalogs -----------------------------------------------------------------------------------------


@pytest.mark.parametrize("language", console_catalog.TARGET_LANGUAGES)
def test_the_catalog_translates_every_console_message(language: str) -> None:
    """Fails when a console text was added or changed without ``manage.py console_messages`` and a
    translation: a missing, empty or obsolete entry, or placeholders that differ from the message's."""
    assert console_catalog.problems(language, console_catalog.collect()) == []


@pytest.mark.parametrize("language", console_catalog.TARGET_LANGUAGES)
def test_the_catalog_is_what_the_command_writes_and_is_compiled(language: str) -> None:
    path = console_catalog.catalog_path(language)
    text = path.read_text(encoding="utf-8")
    entries = {(entry.msgctxt, entry.msgid): entry for entry in parse(text) if entry.msgid}
    assert console_catalog.render(language, console_catalog.collect(), entries) == text
    assert duplicates(parse(text)) == []
    assert path.with_suffix(".mo").read_bytes() == compile_po(text)


def test_a_marked_text_is_itself_under_uzbek() -> None:
    """A console text that is also an English msgid of Django's own Uzbek catalog ("Email") would be replaced
    by that entry under Uzbek. Keep such a word unmarked, or mark it with ``pgettext``. The English interface
    strings our templates reuse on purpose ("Save") are the ones ``locale/uz`` translates."""
    interface = {entry.msgid for entry in console_catalog.read_entries("uz")}
    root = Path(settings.BASE_DIR)
    marked: set[console_catalog.Message] = set()
    for path in console_catalog.python_files():
        marked.update(console_catalog.python_messages(path.read_text(encoding="utf-8")))
    for path in console_catalog.template_files():
        marked.update(console_catalog.template_messages(path.read_text(encoding="utf-8")))
    assert marked, root
    changed = {}
    with translation.override("uz"):
        for message in marked:
            if message.msgctxt is not None or message.msgid in interface:
                continue
            if message.msgid_plural is None:
                shown = [translation.gettext(message.msgid)]
            else:
                shown = [translation.ngettext(message.msgid, message.msgid_plural, n) for n in (1, 2, 5)]
            if set(shown) - {message.msgid, message.msgid_plural}:
                changed[message.msgid] = shown
    assert changed == {}


def test_model_metadata_is_verbatim_under_uzbek_even_when_django_translates_the_word() -> None:
    # Django's uz catalog translates the admin's "None"; the lazy text of a model label never asks it
    lazy_none = console_i18n._in_language_lazy("None")
    with translation.override("uz"):
        assert translation.gettext("None") != "None"
        assert str(lazy_none) == "None"
    with translation.override(None):
        assert str(lazy_none) == "None"
    assert lazy_none == "None"


@pytest.mark.parametrize("domain", ["django", "djangojs"])
def test_russian_covers_every_interface_string_uzbek_covers(domain: str) -> None:
    """Django falls back from Russian to LANGUAGE_CODE: a string missing here would show in Uzbek."""
    uzbek = {(entry.msgctxt, entry.msgid) for entry in console_catalog.read_entries("uz", domain)}
    russian = {
        (entry.msgctxt, entry.msgid)
        for entry in console_catalog.read_entries("ru", domain)
        if all(entry.msgstr.values()) and entry.msgstr
    }
    assert uzbek - russian == set()
    path = console_catalog.catalog_path("ru", domain)
    assert path.with_suffix(".mo").read_bytes() == compile_po(path.read_text(encoding="utf-8"))


def test_the_extractor_reads_python_and_template_markers() -> None:
    python = (
        "from django.utils.translation import gettext as g, gettext_lazy as _, ngettext, pgettext\n"
        'A = _("Holat")\n'
        'def f(n, x):\n    return ngettext("%(n)s ta xabar", "%(n)s ta xabar", n) % {"n": n}, gettext(x), '
        'pgettext("tugma", "Yopish"), translation.gettext("Qator " "ikki")\n'
    )
    assert set(console_catalog.python_messages(python)) == set(
        [
            console_catalog.Message("Holat"),
            console_catalog.Message("%(n)s ta xabar", "%(n)s ta xabar"),
            console_catalog.Message("Yopish", None, "tugma"),
            console_catalog.Message("Qator ikki"),
        ]
    )
    template = (
        '{% load i18n %}<h2>{% translate "Navbat bo‘sh" %}</h2>'
        "<p title=\"{% translate 'Ochish' %}\">{% blocktranslate trimmed with n=count %}\n"
        "  {{ n }} ta provayder, 100% tayyor\n{% endblocktranslate %}</p>{% translate '50% chegirma' %}"
        "{% blocktranslate trimmed count n=total %}{{ n }} ta yozuv{% plural %}{{ n }} ta yozuv"
        "{% endblocktranslate %}"
        '{% include "x.html" with icon="sms" text=_("Kod yuborilmagan") %}{{ _("Bo‘sh")|upper }}'
    )
    assert set(console_catalog.template_messages(template)) == set(
        [
            console_catalog.Message("Navbat bo‘sh"),
            console_catalog.Message("Ochish"),
            # a literal per cent sign is doubled: the msgid the template tags look up
            console_catalog.Message("%(n)s ta provayder, 100%% tayyor"),
            console_catalog.Message("50%% chegirma"),
            console_catalog.Message("%(n)s ta yozuv", "%(n)s ta yozuv"),
            console_catalog.Message("Kod yuborilmagan"),
            console_catalog.Message("Bo‘sh"),
        ]
    )


def test_the_check_command_passes_on_the_committed_catalogs() -> None:
    call_command("console_messages", "--check")
    call_command("compile_locale", "--check")


# --- the pages --------------------------------------------------------------------------------------------


def console_pages() -> list[str]:
    """The dashboards, the settings page, every registered model's list and the change page of its first
    row (the admins of the test's database decide which change pages exist)."""
    pages = [reverse("admin:index"), reverse("admin:site_config_settings")]
    pages += [reverse(f"admin:reporting_{name}") for name in ("money", "sales", "customers")]
    for model in admin.site._registry:
        name = f"{model._meta.app_label}_{model._meta.model_name}"
        pages.append(reverse(f"admin:{name}_changelist"))
        first = model._default_manager.order_by("pk").first()
        if first is not None:
            pages.append(reverse(f"admin:{name}_change", args=[first.pk]))
    return pages


def stored_text() -> str:
    """Every text value the database holds (names, notes, templates, JSON documents): a page shows such a
    value as it was written, in whatever language that was."""
    values: list[str] = []
    for model in apps.get_models():
        columns = [
            field.name
            for field in model._meta.concrete_fields
            if field.get_internal_type() in {"CharField", "TextField", "JSONField", "SlugField", "EmailField"}
        ]
        if columns:
            for row in model._default_manager.values_list(*columns):
                values.extend(str(value) for value in row if value)
    return "\n".join(values)


@pytest.fixture
def demo_shop() -> Iterator[None]:
    """The reporting tests' demo shop: rows on most lists, so their change pages are checked too."""
    from glowderma.core import seeding
    from glowderma.reporting.tests.test_ops_dashboards import NOW, populate

    cache.clear()
    with seeding.pinned_clock(NOW):
        populate()
        yield


@pytest.mark.django_db
@pytest.mark.parametrize("language", ["en", "ru"])
def test_no_console_page_keeps_uzbek_text_in_another_language(language: str, demo_shop: None) -> None:
    """Every dashboard, list and change page: nothing the code writes is spelled the Uzbek way, so no text
    bypasses the catalogs (model and field labels, columns, filters, tiles, tabs, the money and JSON
    fields' headers). What the database holds is data and is set aside."""
    client = with_language(console_client("manager"), language)
    pages = console_pages()
    assert len(pages) > 80
    data = stored_text()
    leftovers: dict[str, list[str]] = {}
    for url in pages:
        page = client.get(url)
        assert page.status_code == 200, url
        assert f'<html lang="{language}"' in page.content.decode()
        text = text_of(page)
        found = sorted(
            {
                text[max(0, match.start() - 25) : match.end() + 25].strip()
                for match in UZBEK.finditer(text)
                if line_of(text, match.start()) not in data and match.group(0) not in data
            }
        )
        if found:
            leftovers[url] = found[:5]
        if language == "ru":
            assert CYRILLIC.search(text), url
    assert leftovers == {}


def line_of(text: str, position: int) -> str:
    start = text.rfind("\n", 0, position) + 1
    end = text.find("\n", position)
    return text[start : end if end != -1 else len(text)].strip()


@pytest.mark.django_db
def test_money_and_json_columns_take_their_header_from_the_lazy_field_label() -> None:
    """``ReadOnlyAdmin`` copies a field's label when the admin is registered: it must be the lazy one."""
    from django.utils.functional import Promise

    order_admin = admin.site._registry[Order]
    label = order_admin.total_som.short_description  # type: ignore[attr-defined]
    assert isinstance(label, Promise)
    with translation.override("en"):
        assert str(label) == "total"
    with translation.override("uz"):
        assert str(label) == "jami"


@pytest.mark.django_db
def test_the_admin_history_names_changed_fields_in_the_viewers_language() -> None:
    """Django stores the changed fields' form labels (Uzbek, capitalised) and shows them through gettext."""
    from django.contrib.admin.models import CHANGE, LogEntry

    from glowderma.professionals.models import Service

    labels = console_catalog.history_texts()
    assert "Standart davomiylik (daq.)" in labels
    entry = LogEntry(
        action_flag=CHANGE, change_message='[{"changed": {"fields": ["Standart davomiylik (daq.)"]}}]'
    )
    with translation.override("uz"):
        assert "Standart davomiylik (daq.)" in entry.get_change_message()
    with translation.override("ru"):
        assert "Standart davomiylik" not in entry.get_change_message()
        assert CYRILLIC.search(entry.get_change_message())
    with translation.override("en"):
        assert "Default duration (min.)" in entry.get_change_message()
    assert str(Service._meta.get_field("default_duration_min").verbose_name) != ""


@pytest.mark.django_db
def test_the_command_palette_follows_a_language_switch() -> None:
    """Unfold caches a user's results per term; a copy made in another language is not served."""
    cache.clear()
    client = console_client("staff")
    url = reverse("admin:search") + "?s=buyurtma"
    uzbek = with_language(client, "uz").get(url).content.decode()
    assert "Buyurtmalar" in uzbek
    english = with_language(client, "en").get(reverse("admin:search") + "?s=order").content.decode()
    assert "Orders" in english
    russian = with_language(client, "ru").get(reverse("admin:search") + "?s=order").content.decode()
    assert "Orders" not in russian


@pytest.mark.django_db
def test_the_uzbek_console_is_the_source_text() -> None:
    page = with_language(console_client("staff"), "uz").get(reverse("admin:index"))
    text = text_of(page)
    assert "Ops holati" in text
    assert "Outbox navbati mavzular bo‘yicha" in text


# --- typing-only names ------------------------------------------------------------------------------------


def _runtime_imports(tree: ast.Module) -> list[tuple[str, str]]:
    """``(module, name)`` of every import outside an ``if TYPE_CHECKING:`` block."""
    found: list[tuple[str, str]] = []

    def visit(node: ast.AST) -> None:
        if isinstance(node, ast.If) and ast.unparse(node.test) in {"TYPE_CHECKING", "typing.TYPE_CHECKING"}:
            for other in node.orelse:
                visit(other)
            return
        if isinstance(node, ast.ImportFrom):
            found.extend((node.module or "", alias.name) for alias in node.names)
        elif isinstance(node, ast.Import):
            found.extend((alias.name, "") for alias in node.names)
        for child in ast.iter_child_nodes(node):
            visit(child)

    visit(tree)
    return found


def test_lazy_string_types_are_imported_for_type_checking_only() -> None:
    """``django_stubs_ext`` and ``django.utils.functional._StrPromise`` come with django-stubs, a development
    dependency: an import of either at runtime passes here and stops the production image from starting."""
    root = Path(settings.BASE_DIR)
    offenders = []
    for path in [*sorted((root / "glowderma").rglob("*.py")), *sorted((root / "config").rglob("*.py"))]:
        if "tests" in path.parts:
            continue
        for module, name in _runtime_imports(ast.parse(path.read_text(encoding="utf-8"))):
            if module.split(".")[0] == "django_stubs_ext" or name in {"_StrPromise", "_StrOrPromise"}:
                offenders.append(f"{path.relative_to(root)}: {module}.{name}")
    assert offenders == []


def test_the_type_checking_guard_sees_through_lookalikes() -> None:
    source = (
        "from typing import TYPE_CHECKING\n"
        "if TYPE_CHECKING:\n    from django_stubs_ext import StrOrPromise\n"
        "else:\n    from django.utils.functional import Promise\n"
        "if not TYPE_CHECKING:\n    import django_stubs_ext\n"
        "def late():\n    from django.utils.functional import _StrPromise\n"
    )
    assert _runtime_imports(ast.parse(source)) == [
        ("typing", "TYPE_CHECKING"),
        ("django.utils.functional", "Promise"),
        ("django_stubs_ext", ""),
        ("django.utils.functional", "_StrPromise"),
    ]
