"""The forms of the ops console settings page ``/django-admin/settings/`` (:mod:`.admin_views`).

The three cards of the former Next.js admin page, one form each, each saved on its own:
«Do‘kon sozlamalari» (:class:`StoreForm`), «Aloqa ma’lumotlari» (:class:`ContactsForm`) and
«Huquqiy matnlar» (:class:`LegalTextsForm`), plus the «E’lon qilish» confirmation (:class:`PublishForm`).
The rules and their Uzbek messages are the ones the admin API enforced (contract §2.11) before the
settings left it for the 2FA console:

* ``reservation_minutes`` 5..60;
* phones ``+998`` and nine digits, or empty (typed as ``+998 71 200 00 00``, or nine local digits);
* links: an absolute ``https`` URL (≤ 200) on a public host name, without credentials, a non-default
  port, quotes, angle brackets, backticks, backslashes, whitespace or control/format characters, scheme
  and host stored lower case; Telegram ``t.me`` / ``telegram.me`` and Instagram ``instagram.com`` with
  one account handle as the path;
* pickup address ≤ 200 per language (trimmed); terms and privacy drafts ≤ 50 000 and the booking
  policy ≤ 5 000 per language, trimmed, line breaks stored as ``\\n``, no visible character ``""``.

**Only what the staff user changed is written.** Each field carries the digest of the value it was
rendered with (``<name>__was``); :meth:`SectionForm.changes` sends :func:`services.update_settings` only
the fields whose cleaned value differs from it — so a page opened before a colleague's save never
writes the colleague's fields back (the Next cards sent only the fields that differed, for that reason).
"""

from __future__ import annotations

import hashlib
import ipaddress
import re
import unicodedata
from typing import TYPE_CHECKING, Any, ClassVar
from urllib.parse import urlsplit, urlunsplit

from django import forms
from django.core.exceptions import ValidationError
from django.core.validators import ProhibitNullCharactersValidator, RegexValidator, URLValidator
from unfold.widgets import UnfoldAdminTextareaWidget, UnfoldAdminTextInputWidget

from glowderma.site_config import services
from glowderma.site_config.models import SiteSettings
from glowderma.site_config.selectors import has_visible_text

if TYPE_CHECKING:
    _TextInput = forms.TextInput
    _Textarea = forms.Textarea
else:
    _TextInput = UnfoldAdminTextInputWidget
    _Textarea = UnfoldAdminTextareaWidget

LANGUAGES = services.LANGUAGES
LANGUAGE_LABELS: dict[str, str] = {"uz": "o‘zbekcha", "ru": "ruscha", "en": "inglizcha"}
LANGUAGE_TABS: dict[str, str] = {"uz": "O‘zbekcha", "ru": "Русский", "en": "English"}

RESERVATION_MIN = 5
RESERVATION_MAX = 60
PICKUP_ADDRESS_MAX = 200
URL_MAX_LENGTH = 200  # SiteSettings.*_url
LEGAL_TEXT_MAX_LENGTH = 50_000  # terms and privacy policy, per language
BOOKING_POLICY_MAX_LENGTH = 5_000
TEXT_MAX_LENGTHS: dict[str, int] = {
    "terms": LEGAL_TEXT_MAX_LENGTH,
    "privacy_policy": LEGAL_TEXT_MAX_LENGTH,
    "booking_policy": BOOKING_POLICY_MAX_LENGTH,
}
DOCUMENT_LABELS: dict[str, str] = {
    "terms": "Foydalanish shartlari",
    "privacy_policy": "Maxfiylik siyosati",
    "booking_policy": "Qabul qoidalari",
}

RESERVATION_INVALID = "5 dan 60 gacha daqiqa kiriting."
RESERVATION_TOO_LOW = "Qiymat 5 dan kam bo‘lmasin."
RESERVATION_TOO_HIGH = "Qiymat 60 dan oshmasin."
PHONE_INVALID = "Telefon raqamini to‘g‘ri kiriting."
URL_INVALID = "Havolani https:// bilan to‘liq kiriting."
TELEGRAM_HOSTS = frozenset({"t.me", "telegram.me", "www.telegram.me"})
TELEGRAM_INVALID = "Telegram havolasi https://t.me/… ko‘rinishida bo‘lsin."
INSTAGRAM_HOSTS = frozenset({"instagram.com", "www.instagram.com"})
INSTAGRAM_INVALID = "Instagram havolasi https://instagram.com/… ko‘rinishida bo‘lsin."
# The account path of a Telegram / Instagram link: one handle segment (Telegram invite links start with "+").
ACCOUNT_PATH = re.compile(r"^/\+?(?=[A-Za-z0-9_.]*[A-Za-z0-9_])[A-Za-z0-9_.]{1,64}/?$")
# Never stored in a link the apps render as an href: quotes, angle brackets, backticks, backslashes.
URL_FORBIDDEN_CHARACTERS = frozenset('"<>`\\')
PHONE_E164 = re.compile(r"^\+998[0-9]{9}$")  # ASCII digits only, as the DB check constraint
# Separators a typed phone may carry ("+998 (71) 200-00-00"); anything else makes it invalid.
PHONE_SEPARATORS = re.compile(r"[\s\-().]")
DIGEST_LENGTH = 16


def too_long(limit: int) -> str:
    """The API's ``max_length`` message (no digit grouping): ``"200 belgidan oshmasin."``."""
    return f"{limit} belgidan oshmasin."


# --- value rules --------------------------------------------------------------------------------------------


def normalize_phone(raw: str) -> str:
    """A typed phone as stored: ``""`` stays ``""``; ``+998 71 200 00 00``, ``998712000000`` and the
    nine local digits ``712000000`` become ``+998712000000``; anything else is :data:`PHONE_INVALID`."""
    value = PHONE_SEPARATORS.sub("", raw.strip())
    if not value:
        return ""
    if value.isascii() and value.isdigit():
        if len(value) == 9:
            value = f"+998{value}"
        elif len(value) == 12 and value.startswith("998"):
            value = f"+{value}"
    if not PHONE_E164.fullmatch(value):
        raise ValidationError(PHONE_INVALID, code="invalid")
    return value


def format_phone(phone: str) -> str:
    """``+998712000000`` → ``+998 71 200 00 00`` (the Next admin's ``formatPhone``); else unchanged."""
    if not PHONE_E164.fullmatch(phone):
        return phone
    digits = phone[4:]
    return f"+998 {digits[:2]} {digits[2:5]} {digits[5:7]} {digits[7:]}"


def _unsafe_url_character(value: str) -> bool:
    """A character no stored link may hold: :data:`URL_FORBIDDEN_CHARACTERS`, whitespace, and control or
    format characters (Unicode ``C*``: bidi overrides, zero-width characters)."""
    return any(
        char in URL_FORBIDDEN_CHARACTERS or char.isspace() or unicodedata.category(char)[0] == "C"
        for char in value
    )


def _public_host(hostname: str) -> bool:
    """A registered-looking host name: not an IP literal and not a single label (``localhost``)."""
    try:
        ipaddress.ip_address(hostname)
    except ValueError:
        return "." in hostname.strip(".")
    return False


def clean_https_url(raw: str, *, hosts: frozenset[str] | None = None, host_message: str = URL_INVALID) -> str:
    """A contact link as stored (``""`` clears it); the rules of the module docstring, in the API's order."""
    value = raw.strip()
    if not value:
        return ""
    if _unsafe_url_character(value):
        raise ValidationError(URL_INVALID, code="invalid")
    try:
        URLValidator(schemes=["https"])(value)
        parts = urlsplit(value)
        port = parts.port
    except ValidationError, ValueError:
        raise ValidationError(URL_INVALID, code="invalid") from None
    hostname = parts.hostname or ""
    if parts.username is not None or parts.password is not None or port not in (None, 443):
        raise ValidationError(URL_INVALID, code="invalid")
    if not _public_host(hostname):
        raise ValidationError(URL_INVALID, code="invalid")
    if hosts is not None and (hostname not in hosts or not ACCOUNT_PATH.match(parts.path)):
        raise ValidationError(host_message, code="invalid")
    if len(value) > URL_MAX_LENGTH:
        raise ValidationError(too_long(URL_MAX_LENGTH), code="max_length")
    return urlunsplit(parts._replace(scheme=parts.scheme.lower(), netloc=parts.netloc.lower()))


def clean_long_text(raw: str, *, limit: int) -> str:
    """A legal text as stored: line breaks as ``\\n``, trimmed, ``""`` without a visible character
    (:func:`selectors.has_visible_text`), at most ``limit`` characters once trimmed."""
    ProhibitNullCharactersValidator()(raw)  # PostgreSQL text cannot store U+0000
    value = raw.replace("\r\n", "\n").replace("\r", "\n").strip()
    if not has_visible_text(value):
        return ""
    if len(value) > limit:
        raise ValidationError(too_long(limit), code="max_length")
    return value


def digest(value: Any) -> str:
    """The short fingerprint a field is rendered with (``<name>__was``): SHA-256 of ``str(value)``."""
    return hashlib.sha256(str(value).encode()).hexdigest()[:DIGEST_LENGTH]


# --- fields -------------------------------------------------------------------------------------------------


class PhoneInput(forms.CharField):
    """A phone typed with spaces; cleaned to ``+998XXXXXXXXX`` or ``""`` (:func:`normalize_phone`)."""

    def __init__(self, *, placeholder: str, **kwargs: Any) -> None:
        attrs = {
            "placeholder": placeholder,
            "inputmode": "tel",
            "autocomplete": "off",
            "data-gd-phone": "",
            "class": "tabular-nums",
        }
        kwargs.setdefault("required", False)
        kwargs.setdefault("max_length", 32)
        kwargs.setdefault("error_messages", {"max_length": PHONE_INVALID})
        widget = _TextInput(attrs=attrs)
        widget.input_type = "tel"
        super().__init__(widget=widget, **kwargs)

    def clean(self, value: Any) -> str:
        return normalize_phone(str(super().clean(value) or ""))


class LinkInput(forms.CharField):
    """A contact link (:func:`clean_https_url`)."""

    def __init__(
        self,
        *,
        placeholder: str,
        hosts: frozenset[str] | None = None,
        host_message: str = URL_INVALID,
        **kwargs: Any,
    ) -> None:
        attrs = {
            "placeholder": placeholder,
            "inputmode": "url",
            "autocomplete": "off",
            "spellcheck": "false",
            "maxlength": str(URL_MAX_LENGTH),
        }
        kwargs.setdefault("required", False)
        # The browser's own maxlength keeps typing in bounds; a longer posted value is refused below.
        widget = _TextInput(attrs=attrs)
        widget.input_type = "url"
        super().__init__(widget=widget, strip=False, **kwargs)
        self.hosts = hosts
        self.host_message = host_message

    def clean(self, value: Any) -> str:
        return clean_https_url(str(value or ""), hosts=self.hosts, host_message=self.host_message)


class LongTextInput(forms.CharField):
    """A legal text (:func:`clean_long_text`); the textarea counts characters against ``limit``."""

    def __init__(self, *, limit: int, label: str, **kwargs: Any) -> None:
        attrs = {"rows": "14", "data-gd-limit": str(limit), "spellcheck": "true"}
        kwargs.setdefault("required", False)
        super().__init__(widget=_Textarea(attrs=attrs), label=label, strip=False, **kwargs)
        self.limit = limit

    def clean(self, value: Any) -> str:
        return clean_long_text(str(value or ""), limit=self.limit)


class ReservationMinutesInput(forms.CharField):
    """Whole minutes 5..60, typed as digits (the Next ``AffixInput`` with the «daqiqa» suffix)."""

    def __init__(self, **kwargs: Any) -> None:
        attrs = {
            "inputmode": "numeric",
            "autocomplete": "off",
            "maxlength": "3",
            "suffix": "daqiqa",
            "class": "tabular-nums",
        }
        super().__init__(
            widget=_TextInput(attrs=attrs),
            error_messages={"required": RESERVATION_INVALID},
            **kwargs,
        )

    def clean(self, value: Any) -> int:
        text = str(super().clean(value) or "").strip()
        if not (text.isascii() and text.isdigit()):
            raise ValidationError(RESERVATION_INVALID, code="invalid")
        minutes = int(text)
        if minutes < RESERVATION_MIN:
            raise ValidationError(RESERVATION_TOO_LOW, code="min_value")
        if minutes > RESERVATION_MAX:
            raise ValidationError(RESERVATION_TOO_HIGH, code="max_value")
        return minutes


# --- forms --------------------------------------------------------------------------------------------------


class SectionForm(forms.Form):
    """One card of the page: its fields map to ``SiteSettings`` columns (:attr:`COLUMNS`); rendered with the
    stored values and their digests, bound to the posted values; :meth:`changes` is what the user changed."""

    section: ClassVar[str] = ""
    title: ClassVar[str] = ""
    saved_message: ClassVar[str] = ""
    # form field → SiteSettings column (services.EDITABLE_FIELDS)
    COLUMNS: ClassVar[dict[str, str]] = {}

    def __init__(self, row: SiteSettings, data: Any = None) -> None:
        self.stored = {name: getattr(row, column) for name, column in self.COLUMNS.items()}
        super().__init__(data=data, initial=self.display_values(), auto_id=f"id_{self.section}_%s")

    def display_values(self) -> dict[str, Any]:
        """The stored values as the inputs show them."""
        return dict(self.stored)

    @property
    def was(self) -> list[tuple[str, str]]:
        """``(<name>__was, digest)`` hidden inputs: the digest of each stored (canonical) value."""
        # A form sent back with errors keeps the digests it was posted with: the baseline stays the page the
        # user opened, not the row as it is now (else a colleague's change would be written back on resubmit).
        posted = self.data if self.is_bound else {}
        return [
            (f"{name}__was", str(posted.get(f"{name}__was") or digest(value)))
            for name, value in self.stored.items()
        ]

    def changes(self) -> dict[str, Any]:
        """Column → new value for the fields whose cleaned value differs from the one rendered (a missing
        digest counts as changed)."""
        if not hasattr(self, "cleaned_data"):
            raise RuntimeError("changes() needs a validated form")
        result: dict[str, Any] = {}
        for name, column in self.COLUMNS.items():
            value = self.cleaned_data[name]
            if digest(value) != self.data.get(f"{name}__was"):
                result[column] = value
        return result

    def first_error_field(self) -> str | None:
        return next((name for name in self.fields if name in self.errors), None)


class StoreForm(SectionForm):
    """«Do‘kon sozlamalari»: the reservation length, the support phone and the pickup address per language."""

    section = "store"
    title = "Do‘kon sozlamalari"
    saved_message = "Sozlamalar saqlandi"
    COLUMNS = {
        "reservation_minutes": "reservation_minutes",
        "support_phone": "support_phone",
        **{f"pickup_address_{language}": f"pickup_address_{language}" for language in LANGUAGES},
    }

    reservation_minutes = ReservationMinutesInput(
        label="Band qilish muddati",
        help_text=(
            "To‘lanmagan buyurtma mahsulotlarni shuncha vaqt ushlab turadi "
            f"({RESERVATION_MIN}–{RESERVATION_MAX})."
        ),
    )
    support_phone = PhoneInput(
        label="Qo‘llab-quvvatlash telefoni",
        placeholder="+998 71 200 00 00",
        help_text="Ilovada ko‘rsatiladi. Bo‘sh qoldirilsa, ko‘rsatilmaydi.",
    )
    pickup_address_uz = forms.CharField(
        label=LANGUAGE_LABELS["uz"],
        required=False,
        max_length=PICKUP_ADDRESS_MAX,
        error_messages={"max_length": too_long(PICKUP_ADDRESS_MAX)},
        widget=_TextInput(attrs={"autocomplete": "off", "maxlength": str(PICKUP_ADDRESS_MAX)}),
    )
    pickup_address_ru = forms.CharField(
        label=LANGUAGE_LABELS["ru"],
        required=False,
        max_length=PICKUP_ADDRESS_MAX,
        error_messages={"max_length": too_long(PICKUP_ADDRESS_MAX)},
        widget=_TextInput(attrs={"autocomplete": "off", "maxlength": str(PICKUP_ADDRESS_MAX)}),
    )
    pickup_address_en = forms.CharField(
        label=LANGUAGE_LABELS["en"],
        required=False,
        max_length=PICKUP_ADDRESS_MAX,
        error_messages={"max_length": too_long(PICKUP_ADDRESS_MAX)},
        widget=_TextInput(attrs={"autocomplete": "off", "maxlength": str(PICKUP_ADDRESS_MAX)}),
    )

    def display_values(self) -> dict[str, Any]:
        values = dict(self.stored)
        values["support_phone"] = format_phone(str(values["support_phone"]))
        return values

    @property
    def pickup_fields(self) -> list[forms.BoundField]:
        return [self[f"pickup_address_{language}"] for language in LANGUAGES]


class ContactsForm(SectionForm):
    """«Aloqa ma’lumotlari»: the store and cosmetologist phones; Telegram, Instagram, website links."""

    section = "contacts"
    title = "Aloqa ma’lumotlari"
    saved_message = "Aloqa ma’lumotlari saqlandi"
    COLUMNS = {name: name for name in services.CONTACT_FIELDS if name != "support_phone"}

    store_phone = PhoneInput(label="Do‘kon telefoni", placeholder="+998 71 200 00 01")
    cosmetologist_phone = PhoneInput(
        label="Kosmetolog telefoni",
        placeholder="+998 71 200 00 02",
        help_text="Ilovaning AI bo‘limida (2-bosqich) ko‘rsatiladi.",
    )
    telegram_url = LinkInput(
        label="Telegram",
        placeholder="https://t.me/glowderma",
        hosts=TELEGRAM_HOSTS,
        host_message=TELEGRAM_INVALID,
    )
    instagram_url = LinkInput(
        label="Instagram",
        placeholder="https://instagram.com/glowderma",
        hosts=INSTAGRAM_HOSTS,
        host_message=INSTAGRAM_INVALID,
    )
    website_url = LinkInput(label="Veb-sayt", placeholder="https://glowderma.uz")

    def display_values(self) -> dict[str, Any]:
        values = dict(self.stored)
        for name in ("store_phone", "cosmetologist_phone"):
            values[name] = format_phone(str(values[name]))
        return values

    @property
    def phone_fields(self) -> list[forms.BoundField]:
        return [self["store_phone"], self["cosmetologist_phone"]]

    @property
    def link_fields(self) -> list[forms.BoundField]:
        return [self["telegram_url"], self["instagram_url"], self["website_url"]]


class LegalTextsForm(SectionForm):
    """«Huquqiy matnlar»: the terms and privacy DRAFTS and the booking policy, per language. Saving never
    publishes: customers see the terms and privacy texts only after «E’lon qilish» (:class:`PublishForm`); the
    booking policy is not versioned and is served once saved."""

    section = "legal"
    title = "Huquqiy matnlar"
    saved_message = "Matnlar saqlandi"
    COLUMNS = {
        f"{document}_{language}": services.text_column(document, language)
        for document in services.TEXT_DOCUMENTS
        for language in LANGUAGES
    }

    def __init__(self, row: SiteSettings, data: Any = None) -> None:
        super().__init__(row, data)
        for document in services.TEXT_DOCUMENTS:
            for language in LANGUAGES:
                self.fields[f"{document}_{language}"] = LongTextInput(
                    limit=TEXT_MAX_LENGTHS[document], label=f"{DOCUMENT_LABELS[document]} ({language})"
                )

    @property
    def documents(self) -> list[dict[str, Any]]:
        """The document tabs: each with its language panels (bound field, limit, whether it has text)."""
        result = []
        for document in services.TEXT_DOCUMENTS:
            panels = []
            for language in LANGUAGES:
                field = self[f"{document}_{language}"]
                value = field.value() or ""
                panels.append(
                    {
                        "language": language,
                        "tab": LANGUAGE_TABS[language],
                        "field": field,
                        "limit": TEXT_MAX_LENGTHS[document],
                        "limit_display": f"{TEXT_MAX_LENGTHS[document]:,}".replace(",", " "),
                        "complete": has_visible_text(str(value)),
                        "error": bool(field.errors),
                    }
                )
            result.append(
                {
                    "key": document,
                    "label": DOCUMENT_LABELS[document],
                    "versioned": document in services.VERSIONED_DOCUMENTS,
                    "panels": panels,
                    "error": any(panel["error"] for panel in panels),
                }
            )
        return result

    def first_error_tab(self) -> tuple[str, str]:
        """The document and language to open: the first one with an error, else the terms in Uzbek."""
        for document in services.TEXT_DOCUMENTS:
            for language in LANGUAGES:
                if f"{document}_{language}" in self.errors:
                    return document, language
        return "terms", "uz"


class PublishForm(forms.Form):
    """«E’lon qilish»: the fingerprint of the drafts and version the staff user saw
    (:func:`services.publish_fingerprint`), so a double click or a page older than a colleague's save or
    publication publishes nothing."""

    fingerprint = forms.CharField(
        max_length=64, widget=forms.HiddenInput, validators=[RegexValidator(r"^[0-9a-f]{64}\Z")]
    )


SECTION_FORMS: dict[str, type[SectionForm]] = {
    form.section: form for form in (StoreForm, ContactsForm, LegalTextsForm)
}
