"""The ops console settings page ``/django-admin/settings/`` (``admin:site_config_settings``,
:mod:`glowderma.site_config.admin_views`, :mod:`glowderma.site_config.forms`): who may open and post it,
what it shows, how each card saves only what the staff member changed, every validation rule of the retired
admin API (``PATCH settings/``) with its Uzbek message, the «E’lon qilish» publication with its fingerprint,
and the services the page writes through (:mod:`glowderma.site_config.services`)."""

from __future__ import annotations

import hashlib
import json
import re
import threading
from collections.abc import Callable
from datetime import UTC, date, datetime
from typing import Any

import pytest
from django import forms as django_forms
from django.core.exceptions import ValidationError
from django.db import connection
from django.test import Client, override_settings
from django.urls import reverse
from rest_framework.test import APIClient

from glowderma.accounts.models import StaffUser
from glowderma.accounts.tests import ops_helpers
from glowderma.accounts.tests.factories import StaffUserFactory
from glowderma.core import timeutils
from glowderma.core.api.exceptions import Conflict
from glowderma.core.models import AuditEntry
from glowderma.core.tests.console import ROLES, console_client, denied, messages_of, user_of
from glowderma.core.timeutils import TASHKENT
from glowderma.site_config import admin_views, services
from glowderma.site_config import forms as settings_forms
from glowderma.site_config.forms import (
    SECTION_FORMS,
    ContactsForm,
    LegalTextsForm,
    SectionForm,
    StoreForm,
    clean_https_url,
    clean_long_text,
    digest,
    format_phone,
    normalize_phone,
)
from glowderma.site_config.models import SiteSettings
from glowderma.site_config.tests.console_page import (
    URL,
    by_testid,
    card_data,
    forms_of,
    page_forms,
    post_card,
    squash,
)
from glowderma.site_config.tests.factories import SiteSettingsFactory

# The forms' messages are lazy translations of their Uzbek text (the console's languages); the suite reads
# the console in Uzbek and compares that text.
INSTAGRAM_INVALID = str(settings_forms.INSTAGRAM_INVALID)
PHONE_INVALID = str(settings_forms.PHONE_INVALID)
RESERVATION_INVALID = str(settings_forms.RESERVATION_INVALID)
RESERVATION_TOO_HIGH = str(settings_forms.RESERVATION_TOO_HIGH)
RESERVATION_TOO_LOW = str(settings_forms.RESERVATION_TOO_LOW)
TELEGRAM_INVALID = str(settings_forms.TELEGRAM_INVALID)
URL_INVALID = str(settings_forms.URL_INVALID)
PUBLIC = "/api/v1/settings/public/"
TERMS_UZ = "1. Umumiy qoidalar.\nGlow Derma ilovasidan foydalanish shartlari."
PRIVACY_UZ = "Shaxsiy ma’lumotlar O‘zbekiston Respublikasi hududida saqlanadi."
CARD_TITLES = {"store": "Do‘kon sozlamalari", "contacts": "Aloqa ma’lumotlari", "legal": "Huquqiy matnlar"}
SAVED = {
    "store": "Sozlamalar saqlandi",
    "contacts": "Aloqa ma’lumotlari saqlandi",
    "legal": "Matnlar saqlandi",
}
LEGAL_NOTE = ". Mijozlar yangi shartlarni e’lon qilganingizdan keyin ko‘radi."
INVISIBLE = ["​", " ​\n", "﻿", "‎‏⁠", " 　", "́"]
Capture = Callable[..., Any]


def at(year: int, month: int, day: int, hour: int = 12, minute: int = 0) -> datetime:
    return datetime(year, month, day, hour, minute, tzinfo=TASHKENT)


def sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:16]


def row() -> SiteSettings:
    return SiteSettings.objects.get(pk=1)


def stored() -> dict[str, Any]:
    """Every column of the singleton (to prove a refused request wrote nothing)."""
    return dict(SiteSettings.objects.values().get(pk=1))


def settings_audit() -> Any:
    """The audit entries the settings page writes (a console sign-in writes its own)."""
    return AuditEntry.objects.filter(action__startswith="settings.")


def updates() -> list[Any]:
    return [entry.after for entry in AuditEntry.objects.filter(action="settings.updated").order_by("id")]


def public(lang: str = "uz") -> dict[str, Any]:
    response = APIClient().get(PUBLIC, HTTP_ACCEPT_LANGUAGE=lang)
    assert response.status_code == 200, response.content
    return dict(response.json())


def fill_drafts(**drafts: str) -> None:
    """Write the terms / privacy **drafts** (``terms_uz=`` → ``terms_draft_uz``) around the services."""
    values = {"terms_uz": TERMS_UZ, "privacy_policy_uz": PRIVACY_UZ, **drafts}
    SiteSettings.objects.filter(pk=1).update(
        **{
            f"{name.rsplit('_', 1)[0]}_draft_{name.rsplit('_', 1)[1]}": value
            for name, value in values.items()
        }
    )


def publish(client: Client, fingerprint: str | None = None) -> Any:
    """Open the page and press «E’lon qilish» with the fingerprint it rendered (or ``fingerprint``)."""
    data = page_forms(client)["publish"]
    if fingerprint is not None:
        data = {**data, "fingerprint": fingerprint}
    return client.post(URL, data)


def published_message(version: str) -> str:
    return f"Shartlar e’lon qilindi: {version}. Mijozlar ilovada yangi versiyani qabul qiladi."


def dialog(content: bytes) -> str:
    """The publish confirmation's markup."""
    return content.decode().split('class="gd-dialog"', 1)[1]


@pytest.fixture
def pinned(monkeypatch: pytest.MonkeyPatch, ops_client: Client) -> list[datetime]:
    """``timeutils.now`` returns the last instant of the list (tests append to move the clock). Depends on
    ``ops_client`` so the console session is stamped with the real clock first: the pinned past instants
    never age it past the idle limit."""
    instants = [at(2026, 10, 20, 10, 0)]
    monkeypatch.setattr(timeutils, "now", lambda: instants[-1])
    return instants


# --- access -------------------------------------------------------------------------------------------------


def test_the_page_is_the_named_console_view() -> None:
    assert reverse(admin_views.URL_NAME) == URL == "/django-admin/settings/"


@pytest.mark.django_db
@pytest.mark.parametrize("role", ROLES)
def test_get_access_matrix(role: str) -> None:
    client = console_client(role)
    response = client.get(URL)
    if role in ("staff", "manager"):
        assert response.status_code == 200
        assert admin_views.TEMPLATE in [template.name for template in response.templates]
    else:
        assert denied(response)
        assert response.status_code == 302 and response["Location"].startswith("/django-admin/login/")
        assert "store" not in (response.context or {})


@pytest.mark.django_db
@pytest.mark.parametrize("role", ROLES)
def test_post_access_matrix(role: str) -> None:
    SiteSettingsFactory.create()
    before = stored()
    client = console_client(role)
    response = client.post(URL, card_data("store", reservation_minutes="30"))
    if role in ("staff", "manager"):
        assert response.status_code == 302, response.content[:500]
        assert row().reservation_minutes == 30
        entry = AuditEntry.objects.get(action="settings.updated")
        assert entry.actor_id == user_of(client).pk
        assert row().updated_by_id == user_of(client).pk
    else:
        assert denied(response)
        assert stored() == before
        assert not settings_audit().exists()


@pytest.mark.django_db
def test_staff_without_the_second_factor_is_sent_to_the_login(client: Client) -> None:
    SiteSettingsFactory.create()
    before = stored()
    client.force_login(ops_helpers.ops_staff())  # the password step only: the session is not verified
    page = client.get(URL)
    assert page.status_code == 302 and page["Location"].startswith("/django-admin/login/")
    for section in SECTION_FORMS:
        refused = client.post(URL, card_data(section, reservation_minutes="30", store_phone="+998712000005"))
        assert refused.status_code == 302 and refused["Location"].startswith("/django-admin/login/")
    fill_drafts()
    before = stored()
    refused = client.post(URL, {"section": "publish", "fingerprint": services.publish_fingerprint(row())})
    assert refused.status_code == 302 and refused["Location"].startswith("/django-admin/login/")
    assert stored() == before
    assert not settings_audit().exists()


@pytest.mark.django_db
def test_csrf_is_enforced() -> None:
    SiteSettingsFactory.create()
    before = stored()
    client = console_client("staff", enforce_csrf_checks=True)
    refused = client.post(URL, card_data("store", reservation_minutes="30"))
    assert refused.status_code == 403
    fill_drafts()
    before = stored()
    refused = client.post(URL, {"section": "publish", "fingerprint": services.publish_fingerprint(row())})
    assert refused.status_code == 403
    assert stored() == before
    assert not settings_audit().exists()
    # the page's own token passes
    accepted = post_card(client, "store", reservation_minutes="30")
    assert accepted.status_code == 302
    assert row().reservation_minutes == 30


@pytest.mark.django_db
@pytest.mark.parametrize("method", ["put", "patch", "delete", "head", "options"])
def test_only_get_and_post_are_allowed(ops_client: Client, method: str) -> None:
    SiteSettingsFactory.create()
    before = stored()
    response = getattr(ops_client, method)(URL)
    assert response.status_code == 405
    assert response["Allow"] == "GET, POST"
    assert stored() == before


@pytest.mark.django_db
def test_the_sidebar_links_the_page_from_the_catalog_group(ops_client: Client) -> None:
    response = ops_client.get(URL)
    groups = {group["title"]: group for group in response.context["sidebar_navigation"]}
    items = {item["title"]: item for item in groups["Katalog"]["items"]}
    assert str(items["Sozlamalar"]["link"]) == URL
    assert items["Sozlamalar"]["icon"] == "tune"
    assert items["Sozlamalar"]["active"] is True  # the open page is highlighted
    every_link = [str(item["link"]) for group in groups.values() for item in group["items"]]
    assert every_link.count(URL) == 1
    assert (
        reverse("admin:site_config_sitesettings_changelist") not in every_link
    )  # the raw view is not listed
    index = ops_client.get(reverse("admin:index"))
    catalog = next(group for group in index.context["sidebar_navigation"] if group["title"] == "Katalog")
    assert next(item for item in catalog["items"] if item["title"] == "Sozlamalar")["active"] is False


@pytest.mark.django_db
def test_the_raw_singleton_stays_read_only_in_the_admin(ops_client: Client) -> None:
    assert ops_client.get(reverse("admin:site_config_sitesettings_change", args=[1])).status_code == 200
    before = stored()
    response = ops_client.post(
        reverse("admin:site_config_sitesettings_change", args=[1]), {"reservation_minutes": "30"}
    )
    assert response.status_code == 403
    assert stored() == before


# --- rendering ----------------------------------------------------------------------------------------------


@pytest.mark.django_db
def test_page_shows_the_three_cards_with_the_stored_values(ops_client: Client) -> None:
    SiteSettingsFactory.create(
        store_phone="+998712000001",
        cosmetologist_phone="+998901112233",
        telegram_url="https://t.me/glowderma",
        instagram_url="https://instagram.com/glowderma",
        website_url="https://glowderma.uz",
        pickup_address_ru="Ташкент, Чиланзар",
        terms_draft_uz=TERMS_UZ,
        privacy_policy_draft_ru="Политика",
        booking_policy_en="Cancel two hours ahead.",
    )
    response = ops_client.get(URL)
    assert response.status_code == 200
    body = response.content.decode()
    for section, title in CARD_TITLES.items():
        assert re.search(rf'<h2 id="{section}-title"[^>]*>\s*{title}\s*</h2>', body), title
        assert f'<section id="{section}"' in body
    forms = forms_of(body)
    assert set(forms) == {"store", "contacts", "legal", "publish"}
    assert {
        name: forms["store"][name]
        for name in ("reservation_minutes", "support_phone", "pickup_address_uz", "pickup_address_ru")
    } == {
        "reservation_minutes": "15",
        "support_phone": "+998 71 200 00 00",
        "pickup_address_uz": "Toshkent, Chilonzor tumani, Bunyodkor shoh ko‘chasi, 12",
        "pickup_address_ru": "Ташкент, Чиланзар",
    }
    assert {name: forms["contacts"][name] for name in ContactsForm.COLUMNS} == {
        "store_phone": "+998 71 200 00 01",
        "cosmetologist_phone": "+998 90 111 22 33",
        "telegram_url": "https://t.me/glowderma",
        "instagram_url": "https://instagram.com/glowderma",
        "website_url": "https://glowderma.uz",
    }
    assert forms["legal"]["terms_uz"] == TERMS_UZ
    assert forms["legal"]["privacy_policy_ru"] == "Политика"
    assert forms["legal"]["booking_policy_en"] == "Cancel two hours ahead."
    assert forms["legal"]["terms_ru"] == "" and forms["legal"]["privacy_policy_uz"] == ""
    # every field carries the digest of its stored value; the publication the fingerprint of what it confirms
    current = row()
    for section, form_class in SECTION_FORMS.items():
        expected = {
            f"{name}__was": digest(getattr(current, column)) for name, column in form_class.COLUMNS.items()
        }
        assert {key: value for key, value in forms[section].items() if key.endswith("__was")} == expected
        assert forms[section]["csrfmiddlewaretoken"]
    assert forms["publish"]["fingerprint"] == services.publish_fingerprint(current)
    # the inputs as the Next page had them
    assert re.search(
        r'<input[^>]*type="tel"[^>]*name="support_phone"|name="support_phone"[^>]*type="tel"', body
    )
    assert re.search(r'name="website_url"[^>]*type="url"|type="url"[^>]*name="website_url"', body)
    assert 'id="id_legal_terms_uz"' in body and 'data-gd-limit="50000"' in body
    assert 'data-gd-limit="5000"' in body
    assert "Band qilish muddati" in body and "daqiqa" in body
    assert response.context["bound_section"] == "" and response.context["focus_field"] is None
    assert (response.context["legal_document"], response.context["legal_language"]) == ("terms", "uz")


@pytest.mark.django_db
def test_the_page_carries_the_console_branding(ops_client: Client) -> None:
    response = ops_client.get(URL)
    body = response.content.decode()
    templates = [template.name for template in response.templates]
    assert templates[0] == admin_views.TEMPLATE
    assert "admin/base_site.html" in templates
    assert re.search(r"<title>(\[[A-Z]+\] )?Sozlamalar \| Glow Derma Ops</title>", body)  # [LOCAL] badge
    assert '<div id="site-name"' in body
    assert "Glow Derma Ops" in body and "Operatsion konsol" in body
    assert "site_config/settings.css" in body and "site_config/settings.js" in body


@pytest.mark.django_db
def test_integrations_badge(ops_client: Client) -> None:
    body = squash(ops_client.get(URL).content)
    assert "Integratsiyalar: sinov (fake)" in body and "Integratsiyalar: jonli" not in body
    assert "gd-badge--warning" in body
    with override_settings(INTEGRATIONS_MODE="live", BTS_MODE="live"):
        live = squash(ops_client.get(URL).content)
    assert "Integratsiyalar: jonli" in live and "Integratsiyalar: sinov" not in live
    assert "gd-badge--success" in live
    assert "bts: live" in live and "click: fake" in live


@pytest.mark.django_db
def test_terms_line_and_unpublished_badge(ops_client: Client) -> None:
    badge = "E’lon qilinmagan o‘zgarishlar bor"
    page = ops_client.get(URL).content
    assert by_testid(page, "terms-version") == "E’lon qilinmagan"
    assert badge not in page.decode()
    # a draft that differs from the (empty) published copy
    fill_drafts()
    page = ops_client.get(URL).content
    assert by_testid(page, "terms-version") == "E’lon qilinmagan"
    assert badge in page.decode()
    # published (21:30 UTC is 02:30 next day in Tashkent): drafts equal the published copies
    SiteSettings.objects.filter(pk=1).update(
        terms_uz=TERMS_UZ,
        privacy_policy_uz=PRIVACY_UZ,
        terms_version="20261021-1",
        terms_updated_at=datetime(2026, 10, 20, 21, 30, tzinfo=UTC),
    )
    page = ops_client.get(URL).content
    assert by_testid(page, "terms-version") == "Joriy versiya: 20261021-1 · e’lon qilingan 21.10.2026 02:30"
    assert badge not in page.decode()
    # the booking policy is not versioned: it never raises the badge
    SiteSettings.objects.filter(pk=1).update(booking_policy_uz="Bekor qilish bepul.")
    assert badge not in ops_client.get(URL).content.decode()
    # any language of either versioned draft does
    for column in ("terms_draft_en", "privacy_policy_draft_ru"):
        SiteSettings.objects.filter(pk=1).update(**{column: "changed"})
        assert badge in ops_client.get(URL).content.decode(), column
        SiteSettings.objects.filter(pk=1).update(**{column: ""})


@pytest.mark.django_db
def test_last_change_line(ops_client: Client, ops_user: StaffUser, pinned: list[datetime]) -> None:
    SiteSettings.objects.filter(pk=1).update(
        updated_at=datetime(2026, 10, 1, 4, 5, tzinfo=UTC), updated_by=None
    )
    assert (
        by_testid(ops_client.get(URL).content, "settings-updated")
        == "Oxirgi o‘zgarish: 01.10.2026 09:05 · Tizim"
    )
    assert post_card(ops_client, "store", reservation_minutes="20").status_code == 302
    assert ops_user.full_name
    assert (
        by_testid(ops_client.get(URL).content, "settings-updated")
        == f"Oxirgi o‘zgarish: 20.10.2026 10:00 · {ops_user.full_name}"
    )
    nameless = StaffUserFactory.create(full_name="", email="nameless@x.uz", is_staff=True)
    SiteSettings.objects.filter(pk=1).update(updated_by=nameless)
    assert by_testid(ops_client.get(URL).content, "settings-updated").endswith("· nameless@x.uz")


@pytest.mark.django_db
def test_publish_dialog_copy_word_for_word(ops_client: Client) -> None:
    assert admin_views.PUBLISH_TITLE == "Shartlarning yangi versiyasini e’lon qilasizmi?"
    assert admin_views.PUBLISH_WARNING == "Barcha mijozlardan shartlarni qayta qabul qilish so‘raladi."
    assert admin_views.PUBLISH_BODY == (
        "Saqlangan foydalanish shartlari va maxfiylik siyosati (barcha tillarda) yangi versiya sifatida "
        "e’lon qilinadi. Mijozlar yangi versiyani qabul qilmaguncha buyurtma bera olmaydi va qabulga "
        "yozila olmaydi. Bu amalni qaytarib bo‘lmaydi."
    )
    response = ops_client.get(URL)
    markup = dialog(response.content)
    assert f'<h2 id="publish-title" class="gd-dialog__title">{admin_views.PUBLISH_TITLE}</h2>' in markup
    assert f'<p class="gd-strong">{admin_views.PUBLISH_WARNING}</p>' in markup
    assert f"<p>{admin_views.PUBLISH_BODY}</p>" in markup
    assert "Bekor qilish" in markup and 'role="alertdialog"' in markup
    assert 'role="alert"' not in markup  # no error until a publication is refused
    assert "publishOpen: false" in response.content.decode()


# --- saving -------------------------------------------------------------------------------------------------


@pytest.mark.django_db
def test_store_card_saves_only_the_changed_fields(
    ops_client: Client, ops_user: StaffUser, django_capture_on_commit_callbacks: Capture
) -> None:
    SiteSettingsFactory.create()
    assert public("ru")["reservation_minutes"] == 15  # cached
    with django_capture_on_commit_callbacks(execute=True):
        response = post_card(
            ops_client,
            "store",
            reservation_minutes="20",
            support_phone="",
            pickup_address_ru="  Ташкент, Чиланзар ",
        )
    assert response.status_code == 302
    assert response["Location"] == "/django-admin/settings/#store"
    assert messages_of(response) == [("success", "Sozlamalar saqlandi")]
    entry = AuditEntry.objects.get(action="settings.updated")
    assert (entry.entity_type, entry.entity_id, entry.actor_id, entry.before) == (
        "settings",
        "1",
        ops_user.pk,
        None,
    )
    assert entry.after == {
        "reservation_minutes": {"from": 15, "to": 20},
        "support_phone": {"from": "+998712000000", "to": None},
        "pickup_address": {"ru": {"from": "", "to": "Ташкент, Чиланзар"}},
    }
    current = row()
    assert (current.reservation_minutes, current.support_phone, current.pickup_address_ru) == (
        20,
        "",
        "Ташкент, Чиланзар",
    )
    assert current.updated_by == ops_user
    # the public settings cache was dropped on commit
    served = public("ru")
    assert (served["reservation_minutes"], served["support_phone"], served["pickup_address"]) == (
        20,
        None,
        "Ташкент, Чиланзар",
    )


@pytest.mark.django_db
def test_contacts_card_round_trip_and_audit(
    ops_client: Client, ops_user: StaffUser, django_capture_on_commit_callbacks: Capture
) -> None:
    SiteSettingsFactory.create()
    public()
    typed = {
        "store_phone": "+998 71 200 00 01",
        "cosmetologist_phone": "998901112233",
        "telegram_url": "  https://t.me/glowderma  ",
        "instagram_url": "https://www.instagram.com/glowderma.uz/",
        "website_url": "https://glowderma.uz",
    }
    expected = {
        "store_phone": "+998712000001",
        "cosmetologist_phone": "+998901112233",
        "telegram_url": "https://t.me/glowderma",
        "instagram_url": "https://www.instagram.com/glowderma.uz/",
        "website_url": "https://glowderma.uz",
    }
    with django_capture_on_commit_callbacks(execute=True):
        response = post_card(ops_client, "contacts", **typed)
    assert response.status_code == 302 and response["Location"] == "/django-admin/settings/#contacts"
    assert messages_of(response) == [("success", "Aloqa ma’lumotlari saqlandi")]
    assert {key: getattr(row(), key) for key in expected} == expected
    assert updates() == [{key: {"from": None, "to": value} for key, value in expected.items()}]
    assert {key: public()[key] for key in expected} == expected
    # the page shows the phones formatted again
    forms = page_forms(ops_client)
    assert (forms["contacts"]["store_phone"], forms["contacts"]["cosmetologist_phone"]) == (
        "+998 71 200 00 01",
        "+998 90 111 22 33",
    )
    # clearing: stored "", audited and served null
    with django_capture_on_commit_callbacks(execute=True):
        cleared = post_card(ops_client, "contacts", store_phone=" ", telegram_url="", website_url="   ")
    assert cleared.status_code == 302
    current = row()
    assert (current.store_phone, current.telegram_url, current.website_url) == ("", "", "")
    assert updates()[-1] == {
        "store_phone": {"from": "+998712000001", "to": None},
        "telegram_url": {"from": "https://t.me/glowderma", "to": None},
        "website_url": {"from": "https://glowderma.uz", "to": None},
    }
    assert [public()[key] for key in ("store_phone", "telegram_url", "website_url")] == [None] * 3
    assert current.updated_by == ops_user


@pytest.mark.django_db
def test_legal_card_trims_audits_digests_and_never_publishes(
    ops_client: Client, django_capture_on_commit_callbacks: Capture
) -> None:
    SiteSettingsFactory.create()
    assert public("ru")["booking_policy"] == ""  # cached
    terms_sent = "  1. Umumiy qoidalar.\r\nGlow Derma ilovasidan foydalanish shartlari.\n\n "
    booking = "Qabulni 2 soat oldin bekor qiling."
    with django_capture_on_commit_callbacks(execute=True):
        response = post_card(
            ops_client,
            "legal",
            terms_uz=terms_sent,
            terms_ru="Условия",
            privacy_policy_uz=PRIVACY_UZ,
            booking_policy_ru=booking,
        )
    assert response.status_code == 302 and response["Location"] == "/django-admin/settings/#legal"
    assert messages_of(response) == [("success", f"Matnlar saqlandi{LEGAL_NOTE}")]
    current = row()
    assert (current.terms_draft_uz, current.terms_draft_ru, current.privacy_policy_draft_uz) == (
        TERMS_UZ,
        "Условия",
        PRIVACY_UZ,
    )
    # saving never publishes: no version, the published copies stay empty
    assert (current.terms_uz, current.privacy_policy_uz, current.terms_version) == ("", "", "")
    assert current.booking_policy_ru == booking
    served = public("ru")
    assert (served["terms"], served["privacy_policy"], served["terms_version"]) == ("", "", None)
    assert served["booking_policy"] == booking  # not versioned: served at once
    assert updates() == [
        {
            "terms": {
                "uz": {"changed": True, "old_len": 0, "new_len": len(TERMS_UZ), "sha256": sha(TERMS_UZ)},
                "ru": {"changed": True, "old_len": 0, "new_len": 7, "sha256": sha("Условия")},
            },
            "privacy_policy": {
                "uz": {"changed": True, "old_len": 0, "new_len": len(PRIVACY_UZ), "sha256": sha(PRIVACY_UZ)}
            },
            "booking_policy": {
                "ru": {"changed": True, "old_len": 0, "new_len": len(booking), "sha256": sha(booking)}
            },
        }
    ]
    logged = json.dumps(updates(), ensure_ascii=False)
    assert "Umumiy" not in logged and "Условия" not in logged and "Qabulni" not in logged  # never the text
    # emptying a text: digest null; the other texts as rendered change nothing
    cleared = post_card(ops_client, "legal", terms_ru="")
    assert cleared.status_code == 302
    assert updates()[-1] == {"terms": {"ru": {"changed": True, "old_len": 7, "new_len": 0, "sha256": None}}}
    assert len(updates()) == 2


@pytest.mark.django_db
def test_legal_message_has_no_note_when_nothing_awaits_publication(ops_client: Client) -> None:
    response = post_card(ops_client, "legal", booking_policy_uz="Bekor qilish bepul.")
    assert messages_of(response) == [("success", "Matnlar saqlandi")]
    fill_drafts()
    noted = post_card(ops_client, "legal", booking_policy_uz="Bekor qilish bepul!")
    assert messages_of(noted) == [("success", f"Matnlar saqlandi{LEGAL_NOTE}")]
    # a draft edited back to the published copy: nothing awaits publication any more
    SiteSettings.objects.filter(pk=1).update(
        terms_uz=TERMS_UZ, privacy_policy_uz=PRIVACY_UZ, terms_draft_uz="x"
    )
    restored = post_card(ops_client, "legal", terms_uz=TERMS_UZ)
    assert messages_of(restored) == [("success", "Matnlar saqlandi")]


@pytest.mark.django_db
@pytest.mark.parametrize("section", list(SECTION_FORMS))
def test_nothing_changed_writes_nothing(ops_client: Client, section: str) -> None:
    SiteSettingsFactory.create(
        store_phone="+998712000001", telegram_url="https://t.me/glowderma", terms_draft_uz=TERMS_UZ
    )
    before = stored()
    response = post_card(ops_client, section)
    assert response.status_code == 302
    assert response["Location"] == f"/django-admin/settings/#{section}"
    assert messages_of(response) == [("info", admin_views.NOTHING_CHANGED)]
    assert admin_views.NOTHING_CHANGED == "O‘zgarish yo‘q: saqlanadigan narsa topilmadi."
    assert stored() == before
    assert not settings_audit().exists()


@pytest.mark.django_db
@pytest.mark.parametrize(
    "section,typed",
    [
        ("store", {"support_phone": "+998712000000"}),
        ("store", {"support_phone": "71 200-00-00", "reservation_minutes": " 15 "}),
        ("store", {"reservation_minutes": "015"}),
        ("contacts", {"store_phone": "998712000001", "telegram_url": "HTTPS://T.ME/glowderma"}),
        ("contacts", {"telegram_url": " https://t.me/glowderma "}),
        ("legal", {"terms_uz": f"\n  {TERMS_UZ.replace(chr(10), chr(13) + chr(10))}  \n"}),
        ("legal", {"terms_ru": " ​ "}),
    ],
)
def test_a_value_typed_differently_but_stored_the_same_is_no_change(
    ops_client: Client, section: str, typed: dict[str, str]
) -> None:
    SiteSettingsFactory.create(
        store_phone="+998712000001", telegram_url="https://t.me/glowderma", terms_draft_uz=TERMS_UZ
    )
    before = stored()
    response = post_card(ops_client, section, **typed)
    assert messages_of(response) == [("info", admin_views.NOTHING_CHANGED)]
    assert stored() == before


@pytest.mark.django_db
def test_the_message_is_shown_after_the_redirect(ops_client: Client) -> None:
    response = ops_client.post(
        URL, {**page_forms(ops_client)["store"], "reservation_minutes": "25"}, follow=True
    )
    assert response.redirect_chain == [("/django-admin/settings/#store", 302)]
    assert "Sozlamalar saqlandi" in response.content.decode()


# --- a stale page -------------------------------------------------------------------------------------------


@pytest.mark.django_db
def test_a_stale_page_never_writes_back_a_colleagues_change(ops_client: Client) -> None:
    SiteSettingsFactory.create()
    forms = page_forms(ops_client)  # rendered with +998712000000
    colleague = StaffUserFactory.create(is_staff=True)
    services.update_settings({"support_phone": "+998712000077", "terms_draft_ru": "Коллега"}, actor=colleague)
    response = ops_client.post(URL, {**forms["store"], "reservation_minutes": "40"})
    assert response.status_code == 302
    assert messages_of(response) == [("success", "Sozlamalar saqlandi")]
    current = row()
    assert (current.support_phone, current.reservation_minutes) == ("+998712000077", 40)
    assert updates()[-1] == {"reservation_minutes": {"from": 15, "to": 40}}
    # the legal card too: the colleague's Russian draft survives an edit of the Uzbek one
    legal = ops_client.post(URL, {**forms["legal"], "terms_uz": TERMS_UZ})
    assert legal.status_code == 302
    current = row()
    assert (current.terms_draft_uz, current.terms_draft_ru) == (TERMS_UZ, "Коллега")
    assert set(updates()[-1]) == {"terms"} and set(updates()[-1]["terms"]) == {"uz"}


@pytest.mark.django_db
def test_a_refused_post_keeps_the_page_baseline(ops_client: Client) -> None:
    SiteSettingsFactory.create()
    forms = page_forms(ops_client)  # rendered with +998712000000
    services.update_settings({"support_phone": "+998712000077"}, actor=None)
    refused = ops_client.post(URL, {**forms["store"], "reservation_minutes": "abc"})
    assert refused.status_code == 200
    again = forms_of(refused.content)["store"]
    assert again["support_phone"] == "+998 71 200 00 00"  # the input as posted
    fixed = ops_client.post(URL, {**again, "reservation_minutes": "40"})
    assert fixed.status_code == 302
    assert row().support_phone == "+998712000077"  # the colleague's phone survives


# --- validation ---------------------------------------------------------------------------------------------

TOO_LONG_200 = "200 belgidan oshmasin."
REFUSED: list[tuple[str, str, str, str]] = [
    # store: reservation minutes
    ("store", "reservation_minutes", "4", RESERVATION_TOO_LOW),
    ("store", "reservation_minutes", "0", RESERVATION_TOO_LOW),
    ("store", "reservation_minutes", "61", RESERVATION_TOO_HIGH),
    ("store", "reservation_minutes", "100", RESERVATION_TOO_HIGH),
    ("store", "reservation_minutes", "abc", RESERVATION_INVALID),
    ("store", "reservation_minutes", "", RESERVATION_INVALID),
    ("store", "reservation_minutes", "   ", RESERVATION_INVALID),
    ("store", "reservation_minutes", "-5", RESERVATION_INVALID),
    ("store", "reservation_minutes", "5.5", RESERVATION_INVALID),
    ("store", "reservation_minutes", "1e1", RESERVATION_INVALID),
    ("store", "reservation_minutes", "２０", RESERVATION_INVALID),  # full-width "20"
    # store: support phone, pickup address
    ("store", "support_phone", "+99871200000", PHONE_INVALID),
    ("store", "support_phone", "+9987120000091", PHONE_INVALID),
    ("store", "support_phone", "+7 912 345 67 89", PHONE_INVALID),
    ("store", "support_phone", "99871200000", PHONE_INVALID),
    ("store", "support_phone", "71200000", PHONE_INVALID),
    ("store", "support_phone", "abc", PHONE_INVALID),
    ("store", "support_phone", "+998 71 200 00 0x", PHONE_INVALID),
    ("store", "support_phone", "+998/71/200/00/00", PHONE_INVALID),
    ("store", "support_phone", "1" * 33, PHONE_INVALID),
    ("store", "pickup_address_uz", "x" * 201, TOO_LONG_200),
    ("store", "pickup_address_ru", "ж" * 201, TOO_LONG_200),
    ("store", "pickup_address_en", "x" * 201, TOO_LONG_200),
    # contacts: phones
    ("contacts", "store_phone", "+99871200000", PHONE_INVALID),
    ("contacts", "cosmetologist_phone", "+998 90 111 22 3", PHONE_INVALID),
    ("contacts", "cosmetologist_phone", "8 901 112 23 34", PHONE_INVALID),
    # contacts: links
    ("contacts", "website_url", "http://glowderma.uz", URL_INVALID),
    ("contacts", "website_url", "glowderma.uz", URL_INVALID),
    ("contacts", "website_url", "https://admin:secret@glowderma.uz/", URL_INVALID),
    ("contacts", "website_url", "https://user@glowderma.uz/", URL_INVALID),
    ("contacts", "website_url", "https://glowderma.uz:8443/", URL_INVALID),
    ("contacts", "website_url", "javascript:alert(1)", URL_INVALID),
    ("contacts", "website_url", "https://glowderma.uz/" + "a" * 180, TOO_LONG_200),
    ("contacts", "telegram_url", "http://t.me/glowderma", URL_INVALID),
    ("contacts", "telegram_url", "https://example.com/glowderma", TELEGRAM_INVALID),
    ("contacts", "telegram_url", "https://t.me.evil.uz/glowderma", TELEGRAM_INVALID),
    ("contacts", "telegram_url", "https://t.me/", TELEGRAM_INVALID),
    ("contacts", "telegram_url", "https://t.me/./", TELEGRAM_INVALID),
    ("contacts", "telegram_url", "https://t.me/glow/derma", TELEGRAM_INVALID),
    ("contacts", "instagram_url", "https://instagr.am/x", INSTAGRAM_INVALID),
    ("contacts", "instagram_url", "https://instagram.com", INSTAGRAM_INVALID),
    ("contacts", "instagram_url", "https://instagram.com/../", INSTAGRAM_INVALID),
    ("contacts", "telegram_url", 'https://t.me/x"><script>alert(1)</script>', URL_INVALID),
    ("contacts", "telegram_url", "https://t.me/x`y", URL_INVALID),
    ("contacts", "telegram_url", "https://t.me/x'y<", URL_INVALID),
    ("contacts", "website_url", "https://glowderma.uz/‮exe", URL_INVALID),
    ("contacts", "website_url", "https://glowderma.uz/​", URL_INVALID),
    ("contacts", "website_url", "https://glowderma.uz/a\\b", URL_INVALID),
    ("contacts", "website_url", "https://glowderma.uz/a b", URL_INVALID),
    ("contacts", "website_url", "https://glowderma.uz/\x07", URL_INVALID),
    ("contacts", "website_url", "https://localhost/", URL_INVALID),
    ("contacts", "website_url", "https://127.0.0.1/", URL_INVALID),
    ("contacts", "website_url", "https://10.0.0.1/", URL_INVALID),
    ("contacts", "website_url", "https://[::1]/", URL_INVALID),
    # legal texts: limits counted after trimming and line-break normalisation
    ("legal", "terms_uz", "x" * 50_001, "50000 belgidan oshmasin."),
    ("legal", "privacy_policy_en", "  " + "x" * 50_001 + "\n", "50000 belgidan oshmasin."),
    ("legal", "terms_ru", "x" * 49_999 + "\r\n" + "xx", "50000 belgidan oshmasin."),
    ("legal", "booking_policy_ru", "x" * 5_001, "5000 belgidan oshmasin."),
]


def _refused_id(row_: tuple[str, str, str, str]) -> str:
    return f"{row_[1]}={row_[2][:24]!r}"


@pytest.mark.django_db
@pytest.mark.parametrize("section,field,value,message", REFUSED, ids=[_refused_id(r) for r in REFUSED])
def test_refused_values(ops_client: Client, section: str, field: str, value: str, message: str) -> None:
    SiteSettingsFactory.create()
    # the form alone
    form = SECTION_FORMS[section](row(), data={**card_data(section), field: value})
    assert not form.is_valid()
    assert form.errors == {field: [message]}
    # the page: re-rendered with the input kept and the error shown, nothing written
    before = stored()
    response = post_card(ops_client, section, **{field: value})
    assert response.status_code == 200
    body = response.content.decode()
    assert message in body
    assert "Maydonlarni tekshiring." in body
    assert response.context[section].errors == {field: [message]}
    assert response.context["bound_section"] == section
    assert response.context["focus_field"] == field
    assert f'data-gd-focus="id_{section}_{field}"' in body
    kept = forms_of(body)[section][field]
    assert kept.replace("\r\n", "\n") == value.replace("\r\n", "\n")
    if section == "legal":
        document, language = field.rsplit("_", 1)
        assert (response.context["legal_document"], response.context["legal_language"]) == (
            document,
            language,
        )
    else:
        assert (response.context["legal_document"], response.context["legal_language"]) == ("terms", "uz")
    assert stored() == before
    assert not settings_audit().exists()
    assert messages_of(response) == []


@pytest.mark.django_db
def test_the_legal_card_opens_the_first_tab_with_an_error(ops_client: Client) -> None:
    response = post_card(
        ops_client,
        "legal",
        booking_policy_en="x" * 5_001,
        privacy_policy_ru="x" * 50_001,
        terms_uz=TERMS_UZ,
    )
    assert response.status_code == 200
    assert (response.context["legal_document"], response.context["legal_language"]) == (
        "privacy_policy",
        "ru",
    )
    assert response.context["focus_field"] == "privacy_policy_ru"
    assert set(response.context["legal"].errors) == {"booking_policy_en", "privacy_policy_ru"}
    body = response.content.decode()
    assert "gdLegal({ bound: 'legal' === 'legal', document: 'privacy_policy', language: 'ru'" in body
    # the other cards keep their stored values
    assert forms_of(body)["store"]["support_phone"] == ""
    assert row().terms_draft_uz == ""  # the valid field of the refused card was not saved either


@pytest.mark.django_db
def test_only_the_posted_card_is_bound(ops_client: Client) -> None:
    SiteSettingsFactory.create(telegram_url="https://t.me/glowderma")
    response = post_card(ops_client, "store", reservation_minutes="abc", support_phone="+998 71 200 00 09")
    assert response.context["store"].is_bound
    assert not response.context["contacts"].is_bound and not response.context["legal"].is_bound
    forms = forms_of(response.content)
    assert forms["store"]["support_phone"] == "+998 71 200 00 09"
    assert forms["contacts"]["telegram_url"] == "https://t.me/glowderma"
    assert response.content.decode().count("Maydonlarni tekshiring.") == 1


ACCEPTED: list[tuple[str, str, str, Any]] = [
    # (section, field, typed, stored)
    ("store", "reservation_minutes", "5", 5),
    ("store", "reservation_minutes", "60", 60),
    ("store", "reservation_minutes", " 30 ", 30),
    ("store", "pickup_address_uz", "x" * 200, "x" * 200),
    ("store", "pickup_address_ru", "  " + "ж" * 200 + " ", "ж" * 200),
    ("contacts", "telegram_url", "https://t.me/glowderma", "https://t.me/glowderma"),
    ("contacts", "telegram_url", "https://telegram.me/glowderma", "https://telegram.me/glowderma"),
    ("contacts", "telegram_url", "https://www.telegram.me/glowderma", "https://www.telegram.me/glowderma"),
    ("contacts", "telegram_url", "https://t.me/+AbC_12", "https://t.me/+AbC_12"),
    ("contacts", "telegram_url", "HTTPS://T.ME/GlowDerma/", "https://t.me/GlowDerma/"),
    ("contacts", "instagram_url", "https://instagram.com/glowderma", "https://instagram.com/glowderma"),
    (
        "contacts",
        "instagram_url",
        "https://www.instagram.com/glowderma?igsh=1",
        "https://www.instagram.com/glowderma?igsh=1",
    ),
    ("contacts", "instagram_url", "https://Instagram.COM/Glow.Derma", "https://instagram.com/Glow.Derma"),
    ("contacts", "website_url", "https://www.glowderma.uz:443/uz/", "https://www.glowderma.uz:443/uz/"),
    ("contacts", "website_url", "https://GlowDerma.UZ/Uz/Page", "https://glowderma.uz/Uz/Page"),
    ("contacts", "website_url", "https://glowderma.uz/" + "a" * 179, "https://glowderma.uz/" + "a" * 179),
    ("legal", "terms_uz", "x" * 50_000, "x" * 50_000),
    ("legal", "privacy_policy_uz", "\n  " + "x" * 50_000 + "  \n", "x" * 50_000),
    ("legal", "terms_en", "x" * 49_998 + "\r\n" + "x", "x" * 49_998 + "\n" + "x"),
    ("legal", "booking_policy_uz", "x" * 5_000, "x" * 5_000),
    ("legal", "booking_policy_en", "a\rb\r\nc", "a\nb\nc"),
    ("legal", "terms_ru", "  Условия \r\n\r\n", "Условия"),
]


@pytest.mark.django_db
@pytest.mark.parametrize("section,field,typed,expected", ACCEPTED, ids=[_refused_id(r) for r in ACCEPTED])
def test_accepted_values(ops_client: Client, section: str, field: str, typed: str, expected: Any) -> None:
    SiteSettingsFactory.create()
    form = SECTION_FORMS[section](row(), data={**card_data(section), field: typed})
    assert form.is_valid(), form.errors
    assert form.cleaned_data[field] == expected
    response = post_card(ops_client, section, **{field: typed})
    assert response.status_code == 302, response.context[section].errors
    assert getattr(row(), SECTION_FORMS[section].COLUMNS[field]) == expected


@pytest.mark.django_db
@pytest.mark.parametrize("text", INVISIBLE)
def test_invisible_texts_are_stored_empty(ops_client: Client, text: str) -> None:
    fill_drafts()
    SiteSettings.objects.filter(pk=1).update(booking_policy_ru="Отмена")
    response = post_card(ops_client, "legal", terms_uz=text, privacy_policy_uz=text, booking_policy_ru=text)
    assert response.status_code == 302
    current = row()
    assert (current.terms_draft_uz, current.privacy_policy_draft_uz, current.booking_policy_ru) == (
        "",
        "",
        "",
    )
    refused = publish(ops_client)
    assert refused.status_code == 200 and refused.context["publish_error"] == services.TERMS_INCOMPLETE


@pytest.mark.django_db
@pytest.mark.parametrize(
    "typed",
    [
        "+998 71 200 00 09",
        "998712000009",
        "712000009",
        "+998 (71) 200-00-09",
        " +998712000009 ",
        "71.200.00.09",
    ],
)
def test_phone_input_forms_are_normalised(ops_client: Client, typed: str) -> None:
    SiteSettingsFactory.create()
    response = post_card(ops_client, "store", support_phone=typed)
    assert response.status_code == 302
    assert row().support_phone == "+998712000009"
    contacts = post_card(ops_client, "contacts", store_phone=typed, cosmetologist_phone=typed)
    assert contacts.status_code == 302
    assert (row().store_phone, row().cosmetologist_phone) == ("+998712000009", "+998712000009")
    assert page_forms(ops_client)["store"]["support_phone"] == "+998 71 200 00 09"


@pytest.mark.django_db
@pytest.mark.parametrize("typed", ["+998７１２０００００９", "+998٧١٢٠٠٠٠٠٩"])
def test_non_ascii_digits_are_refused(ops_client: Client, typed: str) -> None:
    SiteSettingsFactory.create()
    before = stored()
    response = post_card(ops_client, "store", support_phone=typed)
    assert response.status_code == 200
    assert response.context["store"].errors == {"support_phone": [PHONE_INVALID]}
    assert stored() == before


@pytest.mark.django_db
def test_a_null_character_in_a_legal_text_is_a_field_error(ops_client: Client) -> None:
    before = stored()
    response = post_card(ops_client, "legal", terms_uz="Shartlar\x00")
    assert response.status_code == 200
    assert "terms_uz" in response.context["legal"].errors
    assert stored() == before


@pytest.mark.django_db
def test_a_null_character_elsewhere_is_a_field_error(ops_client: Client) -> None:
    before = stored()
    for section, field in (
        ("store", "pickup_address_uz"),
        ("store", "support_phone"),
        ("contacts", "website_url"),
    ):
        response = post_card(ops_client, section, **{field: "https://glowderma.uz/\x00"})
        assert response.status_code == 200, field
        assert field in response.context[section].errors
    assert stored() == before


# --- publish ------------------------------------------------------------------------------------------------


@pytest.mark.django_db
def test_publish_versions_by_tashkent_day_audits_and_invalidates_the_public_cache(
    ops_client: Client,
    ops_user: StaffUser,
    pinned: list[datetime],
    django_capture_on_commit_callbacks: Capture,
) -> None:
    fill_drafts(terms_ru="Условия")
    assert public()["terms_version"] is None  # cached as unpublished
    with django_capture_on_commit_callbacks(execute=True):
        first = publish(ops_client)
    assert first.status_code == 302
    assert first["Location"] == "/django-admin/settings/#legal"
    assert messages_of(first) == [("success", published_message("20261020-1"))]
    current = row()
    assert (current.terms_version, current.terms_updated_at) == ("20261020-1", at(2026, 10, 20, 10, 0))
    assert (current.updated_by, current.updated_at) == (ops_user, at(2026, 10, 20, 10, 0))
    for document in services.VERSIONED_DOCUMENTS:
        for language in services.LANGUAGES:
            assert getattr(current, f"{document}_{language}") == getattr(
                current, f"{document}_draft_{language}"
            )
    assert not services.unpublished_changes(current)
    served = public()
    assert (served["terms_version"], served["terms_updated_at"]) == (
        "20261020-1",
        "2026-10-20T10:00:00+05:00",
    )
    assert (served["terms"], served["privacy_policy"]) == (TERMS_UZ, PRIVACY_UZ)
    assert public("ru")["terms"] == "Условия"
    entry = AuditEntry.objects.get(action="settings.terms_published")
    assert (entry.entity_type, entry.entity_id, entry.actor_id) == ("settings", "1", ops_user.pk)
    assert entry.after == {
        "from": None,
        "to": "20261020-1",
        "sha256": {
            "terms": {"uz": sha(TERMS_UZ), "ru": sha("Условия"), "en": None},
            "privacy_policy": {"uz": sha(PRIVACY_UZ), "ru": None, "en": None},
        },
    }
    page = ops_client.get(URL).content
    assert by_testid(page, "terms-version") == "Joriy versiya: 20261020-1 · e’lon qilingan 20.10.2026 10:00"
    # the same day again: n grows
    pinned.append(at(2026, 10, 20, 23, 59))
    assert messages_of(publish(ops_client)) == [("success", published_message("20261020-2"))]
    # 21:30 UTC on the 20th is the 21st in Tashkent
    pinned.append(datetime(2026, 10, 20, 21, 30, tzinfo=UTC))
    with django_capture_on_commit_callbacks(execute=True):
        third = publish(ops_client)
    assert messages_of(third) == [("success", published_message("20261021-1"))]
    assert public()["terms_updated_at"] == "2026-10-21T02:30:00+05:00"
    log = [
        entry.after or {}
        for entry in AuditEntry.objects.filter(action="settings.terms_published").order_by("id")
    ]
    assert [(data["from"], data["to"]) for data in log] == [
        (None, "20261020-1"),
        ("20261020-1", "20261020-2"),
        ("20261020-2", "20261021-1"),
    ]


@pytest.mark.django_db
def test_edits_after_publishing_stay_drafts_until_the_next_publication(
    ops_client: Client, pinned: list[datetime], django_capture_on_commit_callbacks: Capture
) -> None:
    fill_drafts()
    with django_capture_on_commit_callbacks(execute=True):
        assert publish(ops_client).status_code == 302
        edited = post_card(ops_client, "legal", terms_uz="Butunlay boshqa matn B", privacy_policy_uz=" ")
    assert messages_of(edited) == [("success", f"Matnlar saqlandi{LEGAL_NOTE}")]
    served = public()
    assert (served["terms"], served["privacy_policy"], served["terms_version"]) == (
        TERMS_UZ,
        PRIVACY_UZ,
        "20261020-1",
    )
    # the emptied uz privacy draft blocks the next publication; the published copy stays served
    refused = publish(ops_client)
    assert refused.status_code == 200 and refused.context["publish_error"] == services.TERMS_INCOMPLETE
    assert public()["privacy_policy"] == PRIVACY_UZ
    post_card(ops_client, "legal", privacy_policy_uz=PRIVACY_UZ)
    pinned.append(at(2026, 10, 20, 11, 0))
    with django_capture_on_commit_callbacks(execute=True):
        second = publish(ops_client)
    assert messages_of(second) == [("success", published_message("20261020-2"))]
    served = public()
    assert (served["terms"], served["terms_version"]) == ("Butunlay boshqa matn B", "20261020-2")


@pytest.mark.django_db
@pytest.mark.parametrize(
    "drafts",
    [
        {"terms_uz": "", "privacy_policy_uz": ""},
        {"terms_uz": "   \n "},
        {"privacy_policy_uz": ""},
        {"terms_uz": "", "terms_ru": "Условия", "terms_en": "Terms"},
        {"privacy_policy_uz": "​"},
    ],
)
def test_publish_refuses_without_the_uz_texts(ops_client: Client, drafts: dict[str, str]) -> None:
    fill_drafts(**drafts)
    before = stored()
    response = publish(ops_client)
    assert response.status_code == 200
    assert response.context["publish_error"] == services.TERMS_INCOMPLETE
    assert services.TERMS_INCOMPLETE == (
        "Foydalanish shartlari va maxfiylik siyosatining o‘zbekcha matnini kiriting, so‘ng e’lon qiling."
    )
    markup = dialog(response.content)
    assert services.TERMS_INCOMPLETE in markup and 'role="alert"' in markup
    assert "publishOpen: true" in response.content.decode()
    assert messages_of(response) == []
    assert stored() == before
    assert not AuditEntry.objects.filter(action="settings.terms_published").exists()


@pytest.mark.django_db
def test_a_colleagues_draft_makes_the_publication_stale(ops_client: Client) -> None:
    fill_drafts()
    rendered = page_forms(ops_client)["publish"]
    services.update_settings({"terms_draft_ru": "Новые условия"}, actor=None)
    before = stored()
    response = ops_client.post(URL, rendered)
    assert response.status_code == 200
    assert response.context["publish_error"] == services.PUBLISH_STALE
    assert services.PUBLISH_STALE in dialog(response.content)
    assert "publishOpen: true" in response.content.decode()
    assert stored() == before
    assert not AuditEntry.objects.filter(action="settings.terms_published").exists()
    # the reopened dialog carries the fingerprint of what the page now shows
    assert forms_of(response.content)["publish"]["fingerprint"] == services.publish_fingerprint(row())
    assert forms_of(response.content)["legal"]["terms_ru"] == "Новые условия"


@pytest.mark.django_db
def test_the_same_fingerprint_publishes_once(ops_client: Client) -> None:
    fill_drafts()
    rendered = page_forms(ops_client)["publish"]
    first = ops_client.post(URL, rendered)
    assert first.status_code == 302
    version = row().terms_version
    second = ops_client.post(URL, rendered)  # a double click, or a colleague published meanwhile
    assert second.status_code == 200
    assert second.context["publish_error"] == services.PUBLISH_STALE
    assert row().terms_version == version
    assert AuditEntry.objects.filter(action="settings.terms_published").count() == 1


@pytest.mark.django_db
@pytest.mark.parametrize(
    "data",
    [
        {"section": "publish"},
        {"section": "publish", "fingerprint": ""},
        {"section": "publish", "fingerprint": "a" * 65},
        {"section": "unknown"},
        {"section": ""},
        {},
        {"section": "STORE", "reservation_minutes": "30"},
    ],
)
def test_malformed_posts_are_refused_with_a_message(ops_client: Client, data: dict[str, str]) -> None:
    fill_drafts()
    before = stored()
    response = ops_client.post(URL, data)
    assert response.status_code == 302
    assert response["Location"].startswith(URL)
    assert messages_of(response) == [("error", admin_views.UNKNOWN_SECTION)]
    assert admin_views.UNKNOWN_SECTION == "So‘rov tushunarsiz. Sahifani yangilang va qayta urinib ko‘ring."
    assert stored() == before
    assert not settings_audit().exists()


@pytest.mark.django_db
def test_a_well_formed_but_wrong_fingerprint_is_stale(ops_client: Client) -> None:
    fill_drafts()
    response = publish(ops_client, "0" * 64)
    assert response.status_code == 200 and response.context["publish_error"] == services.PUBLISH_STALE
    assert row().terms_version == ""


@pytest.mark.django_db
def test_a_non_ascii_fingerprint_is_refused_with_a_message(ops_client: Client) -> None:
    fill_drafts()
    response = ops_client.post(URL, {"section": "publish", "fingerprint": "é" * 64})
    assert response.status_code == 302
    assert messages_of(response) == [("error", admin_views.UNKNOWN_SECTION)]
    assert row().terms_version == ""


# --- services -----------------------------------------------------------------------------------------------


def test_leaf_diff_nests_only_changed_leaves() -> None:
    before = {"a": 1, "nested": {"x": "", "y": "keep"}, "phone": None}
    assert services.leaf_diff(before, before) == {}
    assert services.leaf_diff(before, {"a": 2, "nested": {"x": "new", "y": "keep"}, "phone": None}) == {
        "a": {"from": 1, "to": 2},
        "nested": {"x": {"from": "", "to": "new"}},
    }
    assert services.leaf_diff({"phone": "+998712000000"}, {"phone": None}) == {
        "phone": {"from": "+998712000000", "to": None}
    }


@pytest.mark.django_db
@pytest.mark.parametrize("column", ["terms_version", "terms_uz", "updated_at", "id", "terms_draft_xx"])
def test_update_settings_refuses_unknown_columns(ops_user: StaffUser, column: str) -> None:
    before = stored()
    with pytest.raises(ValueError, match=column):
        services.update_settings({column: "2"}, actor=ops_user)
    assert stored() == before
    assert not settings_audit().exists()


def test_every_form_field_maps_to_an_editable_column() -> None:
    columns = [column for form in SECTION_FORMS.values() for column in form.COLUMNS.values()]
    assert sorted(columns) == sorted(services.EDITABLE_FIELDS)
    assert len(columns) == len(set(columns))


@pytest.mark.parametrize(
    "current,today,expected",
    [
        ("", date(2026, 10, 26), "20261026-1"),
        ("20261026-1", date(2026, 10, 26), "20261026-2"),
        ("20261026-9", date(2026, 10, 26), "20261026-10"),
        ("20261025-3", date(2026, 10, 26), "20261026-1"),
        ("20261027-2", date(2026, 10, 26), "20261027-3"),  # the clock went back: never reuse a version
        ("2026-10-01", date(2026, 10, 26), "20261026-1"),  # an earlier format
        ("20261399-1", date(2026, 10, 26), "20261026-1"),
        ("20261026-x", date(2026, 10, 26), "20261026-1"),
        ("２０２６１０２６-1", date(2026, 10, 26), "20261026-1"),
    ],
)
def test_next_terms_version(current: str, today: date, expected: str) -> None:
    assert services.next_terms_version(current, today) == expected


@pytest.mark.django_db
@pytest.mark.parametrize(
    "column",
    [
        "terms_version",
        *(f"{d}_draft_{lang}" for d in ("terms", "privacy_policy") for lang in ("uz", "ru", "en")),
    ],
)
def test_publish_fingerprint_changes_with_the_version_and_each_draft(column: str) -> None:
    fill_drafts()
    current = row()
    fingerprint = services.publish_fingerprint(current)
    assert re.fullmatch(r"[0-9a-f]{64}", fingerprint)
    assert services.publish_fingerprint(row()) == fingerprint  # stable
    setattr(current, column, getattr(current, column) + "!")
    assert services.publish_fingerprint(current) != fingerprint


@pytest.mark.django_db
@pytest.mark.parametrize(
    "column", ["terms_uz", "privacy_policy_ru", "booking_policy_uz", "support_phone", "reservation_minutes"]
)
def test_publish_fingerprint_ignores_what_a_publication_does_not_confirm(column: str) -> None:
    fill_drafts()
    current = row()
    fingerprint = services.publish_fingerprint(current)
    setattr(current, column, 30 if column == "reservation_minutes" else "+998712000001")
    assert services.publish_fingerprint(current) == fingerprint


@pytest.mark.django_db
def test_publish_fingerprint_separates_the_fields() -> None:
    """Moving text from one draft to the next never yields the same fingerprint."""
    current = row()
    current.terms_draft_uz, current.terms_draft_ru = "ab", ""
    first = services.publish_fingerprint(current)
    current.terms_draft_uz, current.terms_draft_ru = "a", "b"
    assert services.publish_fingerprint(current) != first


@pytest.mark.django_db
def test_publish_terms_without_a_fingerprint_still_publishes(ops_user: StaffUser) -> None:
    fill_drafts()
    published = services.publish_terms(actor=ops_user)
    assert published.terms_version.endswith("-1")
    with pytest.raises(Conflict) as stale:
        services.publish_terms(actor=ops_user, expected_fingerprint="0" * 64)
    assert str(stale.value.detail) == services.PUBLISH_STALE
    assert row().terms_version == published.terms_version


def _run_concurrently(target: Callable[[], Any], count: int = 3) -> tuple[list[Any], list[BaseException]]:
    go = threading.Event()
    results: list[Any] = []
    failures: list[BaseException] = []
    lock = threading.Lock()

    def run() -> None:
        try:
            go.wait(10)
            result = target()
            with lock:
                results.append(result)
        except BaseException as exc:  # noqa: BLE001 - reported by the caller's assertions
            with lock:
                failures.append(exc)
        finally:
            connection.close()

    threads = [threading.Thread(target=run) for _ in range(count)]
    for thread in threads:
        thread.start()
    go.set()
    for thread in threads:
        thread.join(timeout=10)
    return results, failures


@pytest.mark.django_db(transaction=True)
def test_concurrent_publications_get_distinct_versions(
    ops_user: StaffUser, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(timeutils, "now", lambda: at(2026, 10, 20, 10, 0))
    fill_drafts()
    results, failures = _run_concurrently(lambda: services.publish_terms(actor=ops_user).terms_version)
    assert not failures
    assert sorted(results) == ["20261020-1", "20261020-2", "20261020-3"]
    assert row().terms_version == "20261020-3"
    assert AuditEntry.objects.filter(action="settings.terms_published").count() == 3


@pytest.mark.django_db(transaction=True)
def test_concurrent_publications_with_the_same_fingerprint_publish_once(
    ops_user: StaffUser, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(timeutils, "now", lambda: at(2026, 10, 20, 10, 0))
    fill_drafts()
    fingerprint = services.publish_fingerprint(row())
    results, failures = _run_concurrently(
        lambda: services.publish_terms(actor=ops_user, expected_fingerprint=fingerprint).terms_version
    )
    assert results == ["20261020-1"]
    assert len(failures) == 2
    assert all(isinstance(exc, Conflict) and str(exc.detail) == services.PUBLISH_STALE for exc in failures)
    assert row().terms_version == "20261020-1"
    assert AuditEntry.objects.filter(action="settings.terms_published").count() == 1


# --- form helpers -------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "typed",
    [
        "+998712000009",
        "+998 71 200 00 09",
        "998712000009",
        "712000009",
        "+998 (71) 200-00-09",
        "  +998\t71 200 00 09 ",
        "71.200.00.09",
    ],
)
def test_normalize_phone_accepts(typed: str) -> None:
    assert normalize_phone(typed) == "+998712000009"


@pytest.mark.parametrize("typed", ["", "   ", " - ( ) . "])
def test_normalize_phone_empty(typed: str) -> None:
    assert normalize_phone(typed) == ""


@pytest.mark.parametrize(
    "typed",
    [
        "+99871200000",
        "+9987120000091",
        "9987120000091",
        "+7 912 345 67 89",
        "+998 71 200 00 0x",
        "8712000009",
        "+ 998712000009x",
        "abc",
        "+998+712000009",
        "0712000009",
    ],
)
def test_normalize_phone_refuses(typed: str) -> None:
    with pytest.raises(ValidationError) as refused:
        normalize_phone(typed)
    assert refused.value.messages == [PHONE_INVALID]


@pytest.mark.parametrize(
    "phone,shown",
    [
        ("+998712000000", "+998 71 200 00 00"),
        ("+998901112233", "+998 90 111 22 33"),
        ("", ""),
        ("+7123", "+7123"),
        ("not a phone", "not a phone"),
    ],
)
def test_format_phone(phone: str, shown: str) -> None:
    assert format_phone(phone) == shown


def test_format_and_normalize_round_trip() -> None:
    for phone in ("+998712000000", "+998901112233", "+998999999999"):
        assert normalize_phone(format_phone(phone)) == phone


def test_clean_https_url() -> None:
    assert clean_https_url("") == "" and clean_https_url("   ") == ""
    assert clean_https_url(" HTTPS://GlowDerma.UZ/A?b=C#D ") == "https://glowderma.uz/A?b=C#D"
    with pytest.raises(ValidationError) as refused:
        clean_https_url("https://x.uz/a", hosts=frozenset({"t.me"}), host_message="custom")
    assert refused.value.messages == ["custom"]
    with pytest.raises(ValidationError) as unsafe:
        clean_https_url('https://t.me/"x', hosts=frozenset({"t.me"}), host_message="custom")
    assert unsafe.value.messages == [URL_INVALID]  # the generic rules first, as the API ordered them
    with pytest.raises(ValidationError) as long_:
        clean_https_url("https://glowderma.uz/" + "a" * 180)
    assert long_.value.messages == ["200 belgidan oshmasin."] and long_.value.code == "max_length"


def test_clean_long_text() -> None:
    assert clean_long_text("  a\r\nb\rc\n\n ", limit=10) == "a\nb\nc"
    assert clean_long_text("​  ", limit=10) == ""
    assert clean_long_text("", limit=10) == ""
    assert clean_long_text("  " + "x" * 10 + "  ", limit=10) == "x" * 10
    assert clean_long_text("x" * 9 + "\r\n", limit=10) == "x" * 9
    with pytest.raises(ValidationError) as refused:
        clean_long_text("x" * 11, limit=10)
    assert refused.value.messages == ["10 belgidan oshmasin."]


def test_digest() -> None:
    assert re.fullmatch(r"[0-9a-f]{16}", digest("x"))
    assert digest(15) == digest("15") == hashlib.sha256(b"15").hexdigest()[:16]
    assert digest("") != digest(" ") and digest("a") != digest("b")


@pytest.mark.django_db
def test_section_form_changes() -> None:
    SiteSettingsFactory.create()
    current = row()
    data = card_data("store")
    form = StoreForm(current, data=data)
    with pytest.raises(RuntimeError):
        form.changes()
    assert form.is_valid(), form.errors
    assert form.changes() == {}
    # a changed value
    form = StoreForm(current, data={**data, "reservation_minutes": "20"})
    assert form.is_valid() and form.changes() == {"reservation_minutes": 20}
    # a missing digest counts as changed (the value is written as posted)
    form = StoreForm(current, data={key: value for key, value in data.items() if key != "support_phone__was"})
    assert form.is_valid() and form.changes() == {"support_phone": "+998712000000"}
    # a forged digest likewise
    form = StoreForm(current, data={**data, "pickup_address_ru__was": "0" * 16})
    assert form.is_valid() and form.changes() == {"pickup_address_ru": ""}


@pytest.mark.django_db
def test_section_forms_render_ids_and_digests_per_card() -> None:
    current = row()
    for section, form_class in SECTION_FORMS.items():
        form: SectionForm = form_class(current)
        assert form.section == section and form.title == CARD_TITLES[section]
        assert form.saved_message == SAVED[section]
        first = next(iter(form.COLUMNS))
        assert form[first].id_for_label == f"id_{section}_{first}"
        assert [name for name, _ in form.was] == [f"{name}__was" for name in form.COLUMNS]
    legal = LegalTextsForm(current)
    assert [doc["key"] for doc in legal.documents] == list(services.TEXT_DOCUMENTS)
    assert [doc["versioned"] for doc in legal.documents] == [True, True, False]
    assert isinstance(legal.fields["terms_uz"], django_forms.CharField)
    assert legal.first_error_tab() == ("terms", "uz")
