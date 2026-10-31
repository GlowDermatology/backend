"""Uzbek ops console: model and app names, the project catalog (locale/uz) and no English leftovers of Unfold
or the Django admin on the pages staff use."""

from __future__ import annotations

import gettext
import io
import re
from html.parser import HTMLParser
from pathlib import Path

import pytest
from django.apps import apps
from django.conf import settings
from django.contrib import admin
from django.core.management import call_command
from django.test import Client
from django.urls import reverse
from django.utils.translation import gettext as _
from django.utils.translation import ngettext

from glowderma.core.models import AuditEntry, OutboxMessage
from glowderma.core.po import compile_po, duplicates, parse
from glowderma.core.tests.console import console_client

CATALOG = Path(settings.BASE_DIR) / "locale" / "uz" / "LC_MESSAGES" / "django.po"

# English strings of Unfold / the Django admin that must not reach a console page (word-bounded).
ENGLISH = (
    "Welcome back to",
    "Log in",
    "Filters",
    "Type to search",
    "Search apps and models",
    "General",
    "History",
    "Close",
    "All applications",
    "Show all",
    "results",
    "result",
    "total",
    "to view",
    "Change history",
    "Recent actions",
    "Home",
    "Apply Filters",
    "No results found",
    "Select action",
    "Cancel",
    "Submit",
    "View",
    "Password",
    "Toggle",
    "Previous",
    "Next",
)
# Model names Django would derive from the class name (``"auth session"``) must not be shown either.
ENGLISH_MODEL_NAMES = ("audit entry", "outbox message", "idempotency record", "auth session", "staff user")


class _VisibleText(HTMLParser):
    """Text a person sees: element text plus ``title`` / ``placeholder`` / ``aria-label``, no scripts."""

    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in ("script", "style", "template"):
            self._skip += 1
        for name, value in attrs:
            if name in ("title", "placeholder", "aria-label") and value:
                self.parts.append(value)

    def handle_endtag(self, tag: str) -> None:
        if tag in ("script", "style", "template") and self._skip:
            self._skip -= 1

    def handle_data(self, data: str) -> None:
        if not self._skip and data.strip():
            self.parts.append(data.strip())


def visible_text(html: bytes) -> str:
    parser = _VisibleText()
    parser.feed(html.decode())
    return "\n".join(parser.parts)


def english_leftovers(html: bytes) -> list[str]:
    text = visible_text(html)
    found = [word for word in ENGLISH if re.search(rf"\b{re.escape(word)}\b", text)]
    found += [name for name in ENGLISH_MODEL_NAMES if name in text.lower()]
    return found


# --- pages -------------------------------------------------------------------------------------------------


@pytest.mark.django_db
def test_login_page_is_uzbek() -> None:
    page = Client().get(reverse("admin:login"))
    assert page.status_code == 200
    assert english_leftovers(page.content) == []
    assert "Kirish" in visible_text(page.content)


@pytest.mark.django_db
def test_index_changelist_change_and_history_pages_are_uzbek() -> None:
    dead = OutboxMessage.objects.create(topic="t.x", payload={}, dedupe_key="i18n-1", status="dead")
    entry = AuditEntry.objects.create(actor_type="system", action="x.y", entity_type="outbox", entity_id="1")
    client = console_client("manager")
    pages = {
        "index": reverse("admin:index"),
        "app index": reverse("admin:app_list", args=["core"]),
        "outbox list": reverse("admin:core_outboxmessage_changelist"),
        "outbox list (filtered, empty)": reverse("admin:core_outboxmessage_changelist") + "?q=nothing-here",
        "outbox change": reverse("admin:core_outboxmessage_change", args=[dead.pk]),
        "outbox history": reverse("admin:core_outboxmessage_history", args=[dead.pk]),
        "audit list": reverse("admin:core_auditentry_changelist"),
        "audit change": reverse("admin:core_auditentry_change", args=[entry.pk]),
        "action confirm": reverse("admin:core_outboxmessage_requeue", args=[dead.pk]),
    }
    for name, url in pages.items():
        page = client.get(url)
        assert page.status_code == 200, name
        assert english_leftovers(page.content) == [], name


@pytest.mark.django_db
def test_change_page_title_is_readable_not_the_english_str() -> None:
    dead = OutboxMessage.objects.create(topic="t.x", payload={}, dedupe_key="i18n-2", status="dead")
    page = console_client("staff").get(reverse("admin:core_outboxmessage_change", args=[dead.pk]))
    text = page.content.decode()
    assert f"Outbox xabari #{dead.pk} | Outbox xabari | Glow Derma Ops</title>" in text
    assert str(dead) not in visible_text(page.content)  # "t.x: i18n-2 (dead)"
    assert page.context["original"].pk == dead.pk
    confirm_page = console_client("staff").get(reverse("admin:core_outboxmessage_requeue", args=[dead.pk]))
    assert f"Outbox xabari #{dead.pk}" in confirm_page.content.decode()
    assert str(dead) not in visible_text(confirm_page.content)


# --- names -------------------------------------------------------------------------------------------------

UZBEK_LETTERS = re.compile(r"[og]'", re.IGNORECASE)  # o' / g' must use the turned comma ‘ (U+2018)


def test_every_project_model_in_the_console_has_an_uzbek_name() -> None:
    for model in admin.site._registry:
        if not model.__module__.startswith("glowderma."):
            continue  # django_otp's TOTPDevice keeps its own names (third-party migrations)
        opts = model._meta
        assert {"verbose_name", "verbose_name_plural"} <= set(opts.original_attrs), model
        for name in (str(opts.verbose_name), str(opts.verbose_name_plural)):
            assert not UZBEK_LETTERS.search(name), name


# Values whose Uzbek label is the same proper name as the title-cased value (``click`` → «Click»).
SAME_IN_UZBEK = {
    "admin", "android", "apple", "billz", "bts", "click", "eskiz", "facebook", "fcm", "google", "payme",
    "segment", "webhook",
}  # fmt: skip


def test_every_choice_shown_in_the_console_has_an_uzbek_label() -> None:
    """Choice filters, status cells and change pages read ``field.flatchoices``: no Django-derived English
    label (``"Handed To Courier"``) and no raw value. The stored values and the API stay the literals."""
    checked = 0
    for model in admin.site._registry:
        if not model.__module__.startswith("glowderma."):
            continue  # django_otp's TOTPDevice.digits (6 / 8) keeps its own choices
        for field in model._meta.concrete_fields:
            for value, label in field.flatchoices:
                checked += 1
                if value in SAME_IN_UZBEK:
                    continue
                text = str(label)
                assert text not in (str(value), str(value).replace("_", " ").title()), (model, field.name)
                assert not UZBEK_LETTERS.search(text), text
    assert checked > 200


def test_choice_labels_repeat_the_export_labels() -> None:
    from glowderma.core import enums, labels

    pairs = (
        (labels.ORDER_STATUS, enums.OrderStatus),
        (labels.PAYMENT_STATUS, enums.PaymentStatus),
        (labels.FULFILMENT, enums.FulfilmentMethod),
        (labels.CANCELLATION_REASON, enums.CancellationReason),
        (labels.PRODUCT_STATUS, enums.ProductStatus),
        (labels.CUSTOMER_STATUS, enums.CustomerStatus),
        (labels.LEDGER_KIND, enums.LedgerKind),
        (labels.NOTIFICATION_KIND, enums.NotificationKind),
        (labels.SPECIALIST_ROLE, enums.SpecialistRole),
        (labels.SERVICE_CATEGORY, enums.ServiceCategory),
        (labels.REFUND_STATE, enums.RefundState),
    )
    for mapping, enum in pairs:
        choices = dict(enum.choices)
        assert {value: choices[value] for value in mapping} == mapping, enum.__name__


@pytest.mark.django_db
def test_choice_filter_and_status_cell_show_the_uzbek_label() -> None:
    OutboxMessage.objects.create(topic="t.x", payload={}, dedupe_key="i18n-3", status="dead")
    page = console_client("staff").get(reverse("admin:core_outboxmessage_changelist"))
    text = visible_text(page.content)
    assert "O‘lik" in text and "Yuborilmoqda" in text  # the status cell and the dropdown filter options
    assert "In Flight" not in text and "Dead" not in text


def test_common_fields_are_labelled_in_uzbek() -> None:
    expected = {
        "created_at": "yaratilgan",
        "updated_at": "yangilangan",
        "status": "holat",
        "customer": "mijoz",
    }
    seen: dict[str, set[str]] = {}
    for model in apps.get_app_config("commerce").get_models():
        for field in model._meta.concrete_fields:
            if field.name in expected:
                seen.setdefault(field.name, set()).add(str(field.verbose_name))
    assert seen == {name: {label} for name, label in expected.items()}


def test_project_apps_have_uzbek_labels() -> None:
    labels = {config.label: str(config.verbose_name) for config in apps.get_app_configs()}
    assert labels["commerce"] == "Savdo"
    assert labels["catalog"] == "Katalog"
    assert labels["accounts"] == "Hisoblar"
    assert labels["payments"] == "To‘lov va qaytarishlar"  # never the same as its model list (breadcrumbs)
    # django_otp's device apps (no verbose_name of their own: the index showed "Otp_Totp")
    assert labels["otp_totp"] == "Ikki bosqichli kirish (TOTP)"
    from glowderma.core.otp_apps import OtpStaticConfig

    assert OtpStaticConfig.verbose_name == "Tiklash kodlari"
    assert OtpStaticConfig.name == "django_otp.plugins.otp_static"
    for config in apps.get_app_configs():
        if config.name.startswith("glowderma."):
            assert config.verbose_name != config.label.replace("_", " ").title(), config.name


# --- the catalog -------------------------------------------------------------------------------------------


def test_the_committed_mo_matches_the_po() -> None:
    call_command("compile_locale", "--check", stdout=io.StringIO())
    assert CATALOG.with_suffix(".mo").read_bytes() == compile_po(CATALOG.read_text(encoding="utf-8"))


def test_the_compiled_catalog_reads_back_with_gettext() -> None:
    translations = gettext.GNUTranslations(io.BytesIO(compile_po(CATALOG.read_text(encoding="utf-8"))))
    for entry in parse(CATALOG.read_text(encoding="utf-8")):
        if not entry.msgid:
            continue
        if entry.msgctxt is not None:
            assert translations.pgettext(entry.msgctxt, entry.msgid) == entry.msgstr[0], entry.msgid
        elif entry.msgid_plural is None:
            assert translations.gettext(entry.msgid) == entry.msgstr[0], entry.msgid
        else:
            assert translations.ngettext(entry.msgid, entry.msgid_plural, 2) == entry.msgstr[0]
    assert translations.info()["plural-forms"] == "nplurals=1; plural=0;"


def test_catalog_uses_the_uzbek_apostrophes() -> None:
    for entry in parse(CATALOG.read_text(encoding="utf-8")):
        for text in entry.msgstr.values():
            assert not UZBEK_LETTERS.search(text), text


def test_django_uses_the_project_catalog() -> None:
    assert _("Log in") == "Kirish"
    assert _("Change") == "O‘zgartirish"  # overrides Django's own "O'zgartirish"
    assert ngettext("%(counter)s result", "%(counter)s results", 5) == "%(counter)s ta yozuv"


def test_po_parser_handles_context_plurals_fuzzy_and_continuations() -> None:
    text = (
        'msgid ""\nmsgstr ""\n"Content-Type: text/plain; charset=UTF-8\\n"\n'
        '"Plural-Forms: nplurals=2; plural=(n != 1);\\n"\n\n'
        'msgctxt "menu"\nmsgid "Open"\nmsgstr "Och"\n\n'
        '#, fuzzy\nmsgid "Draft"\nmsgstr "Qoralama"\n\n'
        'msgid "Long "\n"line"\nmsgstr "Uzun "\n"qator \\"x\\""\n\n'
        'msgid "one"\nmsgid_plural "many"\nmsgstr[0] "bitta"\nmsgstr[1] "ko‘p"\n\n'
        'msgid "untranslated"\nmsgstr ""\n'
    )
    translations = gettext.GNUTranslations(io.BytesIO(compile_po(text)))
    assert translations.pgettext("menu", "Open") == "Och"
    assert translations.gettext("Draft") == "Draft"  # fuzzy: left out
    assert translations.gettext("Long line") == 'Uzun qator "x"'
    assert translations.ngettext("one", "many", 1) == "bitta"
    assert translations.ngettext("one", "many", 3) == "ko‘p"
    assert translations.gettext("untranslated") == "untranslated"


def test_a_message_defined_twice_is_refused_like_msgfmt() -> None:
    header = 'msgid ""\nmsgstr ""\n"Content-Type: text/plain; charset=UTF-8\\n"\n\n'
    twice = header + 'msgid "Save"\nmsgstr "Saqlash"\n\nmsgid "Save"\nmsgid_plural "Saves"\nmsgstr[0] "x"\n'
    with pytest.raises(ValueError, match="duplicate message definition: 'Save'"):
        compile_po(twice)
    in_contexts = header + 'msgctxt "a"\nmsgid "Save"\nmsgstr "A"\n\nmsgctxt "b"\nmsgid "Save"\nmsgstr "B"\n'
    assert compile_po(in_contexts)  # another context is another message


@pytest.mark.parametrize("name", ["django.po", "djangojs.po"])
def test_the_committed_catalogs_define_every_message_once(name: str) -> None:
    assert duplicates(parse((CATALOG.parent / name).read_text(encoding="utf-8"))) == []


# --- gaps found by the browser suite (e2e_admin/tests/uzbek.spec.ts, mutating/professionals.spec.ts) --------


def test_django_otp_device_models_have_uzbek_names_without_a_migration() -> None:
    from django_otp.plugins.otp_static.models import StaticDevice
    from django_otp.plugins.otp_totp.models import TOTPDevice

    assert (str(TOTPDevice._meta.verbose_name), str(TOTPDevice._meta.verbose_name_plural)) == (
        "2FA qurilma",
        "2FA qurilmalar",
    )
    assert str(StaticDevice._meta.verbose_name_plural) == "tiklash kodlari to‘plamlari"
    # what the migration autodetector reads stays django_otp's own value
    assert TOTPDevice._meta.original_attrs["verbose_name"] == "TOTP device"
    assert "verbose_name" not in StaticDevice._meta.original_attrs


@pytest.mark.parametrize(
    "message",
    [
        "The {name} “{obj}” was added successfully. You may edit it again below.",
        "The {name} “{obj}” was added successfully. You may add another {name} below.",
        "The {name} “{obj}” was changed successfully. You may edit it again below.",
        "The {name} “{obj}” was changed successfully. You may add another {name} below.",
        "The %(name)s “%(obj)s” was deleted successfully.",
        "Cannot delete %(name)s",
        'Are you sure you want to delete the %(object_name)s "%(escaped_object)s"? All of the following '
        "related items will be deleted:",
        "Deleting the %(object_name)s '%(escaped_object)s' would result in deleting related objects, but "
        "your account doesn't have permission to delete the following types of objects:",
        "Deleting the %(object_name)s '%(escaped_object)s' would require deleting the following protected "
        "related objects:",
    ],
)
def test_the_editable_forms_messages_are_uzbek(message: str) -> None:
    """The professionals pages' save / delete messages: Django's uz catalog leaves these untranslated."""
    from django.utils import translation

    with translation.override("uz"):
        translated = _(message)
    assert translated != message
    for placeholder in re.findall(r"\{\w+\}|%\(\w+\)s", message):
        assert placeholder in translated


@pytest.mark.django_db
def test_the_admin_javascript_catalog_is_uzbek() -> None:
    """The date and time widgets' labels (the «Bugun» shortcut is announced as "Today (…)" otherwise)."""
    page = console_client("staff").get("/django-admin/jsi18n/")
    assert page.status_code == 200
    text = page.content.decode()
    for msgid, msgstr in (
        ("Today (%(date)s)", "Bugun (%(date)s)"),
        ("Yesterday (%(date)s)", "Kecha (%(date)s)"),
        ("Previous (%(month)s %(year)s)", "Oldingi oy (%(month)s %(year)s)"),
        ("Monday", "Dushanba"),
    ):
        assert f'"{msgid}": "{msgstr}"' in text


def test_the_professionals_form_fields_are_labelled_in_uzbek() -> None:
    from glowderma.professionals.models import Location, Professional

    assert str(Professional._meta.get_field("location").verbose_name) == "filial"
    assert str(Location._meta.get_field("latitude").verbose_name) == "kenglik"
    for model in apps.get_app_config("professionals").get_models():
        for field in model._meta.concrete_fields:
            if field.editable and not field.primary_key:
                assert str(field.verbose_name) != field.name.replace("_", " "), (model, field.name)


def test_the_javascript_catalog_is_compiled_and_uses_the_uzbek_apostrophes() -> None:
    js = CATALOG.with_name("djangojs.po")
    assert js.with_suffix(".mo").read_bytes() == compile_po(js.read_text(encoding="utf-8"))
    for entry in parse(js.read_text(encoding="utf-8")):
        for text in entry.msgstr.values():
            assert not UZBEK_LETTERS.search(text), text
