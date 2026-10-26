"""Contract §2.11 (gaps X-SET-CONTACTS, X-SET-LEGAL): the contact fields and the legal texts of ``PATCH
settings/``, their audited digests, and ``POST settings/terms/publish/`` (version, refusals, idempotency, the
public-settings cache, concurrent publications)."""

from __future__ import annotations

import hashlib
import json
import threading
import uuid
from datetime import date, datetime
from typing import Any, cast

import pytest
from django.db import connection
from rest_framework.test import APIClient

from glowderma.accounts.models import StaffUser
from glowderma.core import timeutils
from glowderma.core.models import AuditEntry
from glowderma.core.timeutils import TASHKENT
from glowderma.site_config import selectors, services
from glowderma.site_config.models import SiteSettings
from glowderma.site_config.tests.factories import SiteSettingsFactory

URL = "/api/v1/admin/settings/"
PUBLISH = "/api/v1/admin/settings/terms/publish/"
PUBLIC = "/api/v1/settings/public/"
TERMS_UZ = "1. Umumiy qoidalar.\nGlow Derma ilovasidan foydalanish shartlari."
PRIVACY_UZ = "Shaxsiy ma’lumotlar O‘zbekiston Respublikasi hududida saqlanadi."


def at(year: int, month: int, day: int, hour: int = 12, minute: int = 0) -> datetime:
    return datetime(year, month, day, hour, minute, tzinfo=TASHKENT)


def errors(response: Any) -> list[tuple[str | None, str, str]]:
    return [(e["field"], e["code"], e["message"]) for e in response.data["errors"]]


def sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:16]


def publish(client: APIClient, key: str | None = None) -> Any:
    return client.post(PUBLISH, format="json", HTTP_IDEMPOTENCY_KEY=key or str(uuid.uuid4()))


def fill_texts(**overrides: str) -> None:
    values = {"terms_uz": TERMS_UZ, "privacy_policy_uz": PRIVACY_UZ, **overrides}
    SiteSettings.objects.filter(pk=1).update(**values)


@pytest.fixture
def pinned(monkeypatch: pytest.MonkeyPatch) -> list[datetime]:
    """``timeutils.now`` returns the last instant of the list (tests append to move the clock)."""
    instants = [at(2026, 10, 20, 10, 0)]
    monkeypatch.setattr(timeutils, "now", lambda: instants[-1])
    return instants


# --- contacts ---------------------------------------------------------------------------------------------


@pytest.mark.django_db
def test_patch_contacts_round_trip_and_audit(staff_client: APIClient, staff: StaffUser) -> None:
    SiteSettingsFactory.create()
    body = {
        "store_phone": "+998712000001",
        "cosmetologist_phone": "+998901112233",
        "telegram_url": "  https://t.me/glowderma  ",
        "instagram_url": "https://www.instagram.com/glowderma.uz/",
        "website_url": "https://glowderma.uz",
    }
    response = staff_client.patch(URL, body, format="json")
    assert response.status_code == 200, response.data
    expected = {**body, "telegram_url": "https://t.me/glowderma"}
    assert {key: response.data[key] for key in body} == expected
    entry = AuditEntry.objects.get(action="settings.updated")
    assert entry.actor_id == staff.pk
    assert entry.after == {key: {"from": None, "to": value} for key, value in expected.items()}
    # null and "" both clear a contact: stored "", served null
    cleared = staff_client.patch(
        URL, {"store_phone": None, "telegram_url": "", "website_url": None}, format="json"
    )
    assert cleared.status_code == 200, cleared.data
    assert [cleared.data[key] for key in ("store_phone", "telegram_url", "website_url")] == [None] * 3
    row = SiteSettings.objects.get(pk=1)
    assert (row.store_phone, row.telegram_url, row.website_url) == ("", "", "")
    assert AuditEntry.objects.filter(action="settings.updated").latest("id").after == {
        "store_phone": {"from": "+998712000001", "to": None},
        "telegram_url": {"from": "https://t.me/glowderma", "to": None},
        "website_url": {"from": "https://glowderma.uz", "to": None},
    }


URL_INVALID = "Havolani https:// bilan to‘liq kiriting."
TELEGRAM_INVALID = "Telegram havolasi https://t.me/… ko‘rinishida bo‘lsin."
INSTAGRAM_INVALID = "Instagram havolasi https://instagram.com/… ko‘rinishida bo‘lsin."


@pytest.mark.django_db
@pytest.mark.parametrize(
    "body,expected",
    [
        ({"store_phone": "+99871200000"}, [("store_phone", "invalid", "Telefon raqamini to‘g‘ri kiriting.")]),
        (
            {"cosmetologist_phone": "998901112233"},
            [("cosmetologist_phone", "invalid", "Telefon raqamini to‘g‘ri kiriting.")],
        ),
        ({"website_url": "http://glowderma.uz"}, [("website_url", "invalid", URL_INVALID)]),
        ({"website_url": "glowderma.uz"}, [("website_url", "invalid", URL_INVALID)]),
        ({"website_url": "https://admin:secret@glowderma.uz/"}, [("website_url", "invalid", URL_INVALID)]),
        ({"website_url": "https://glowderma.uz:8443/"}, [("website_url", "invalid", URL_INVALID)]),
        ({"website_url": "javascript:alert(1)"}, [("website_url", "invalid", URL_INVALID)]),
        (
            {"website_url": "https://glowderma.uz/" + "a" * 180},
            [("website_url", "max_length", "200 belgidan oshmasin.")],
        ),
        ({"telegram_url": "http://t.me/glowderma"}, [("telegram_url", "invalid", URL_INVALID)]),
        ({"telegram_url": "https://example.com/glowderma"}, [("telegram_url", "invalid", TELEGRAM_INVALID)]),
        ({"telegram_url": "https://t.me.evil.uz/glowderma"}, [("telegram_url", "invalid", TELEGRAM_INVALID)]),
        ({"telegram_url": "https://t.me/"}, [("telegram_url", "invalid", TELEGRAM_INVALID)]),
        ({"instagram_url": "https://instagr.am/x"}, [("instagram_url", "invalid", INSTAGRAM_INVALID)]),
        ({"instagram_url": "https://instagram.com"}, [("instagram_url", "invalid", INSTAGRAM_INVALID)]),
        (
            {"terms": {"uz": "x" * 50_001}},
            [("terms.uz", "max_length", "50000 belgidan oshmasin.")],
        ),
        (
            {"booking_policy": {"ru": "x" * 5_001}},
            [("booking_policy.ru", "max_length", "5000 belgidan oshmasin.")],
        ),
        ({"privacy_policy": None}, [("privacy_policy", "null", "Majburiy maydon.")]),
        ({"terms_version": "1"}, [("terms_version", "read_only", "Bu maydonni o‘zgartirib bo‘lmaydi.")]),
        (
            {"terms_updated_at": "2026-10-01T00:00:00+05:00"},
            [("terms_updated_at", "read_only", "Bu maydonni o‘zgartirib bo‘lmaydi.")],
        ),
    ],
)
def test_patch_validation_rows(staff_client: APIClient, body: dict[str, Any], expected: list[Any]) -> None:
    SiteSettingsFactory.create()
    response = staff_client.patch(URL, body, format="json")
    assert response.status_code == 400, response.data
    assert response.data["code"] == "validation_error"
    assert errors(response) == expected
    assert not AuditEntry.objects.filter(action="settings.updated").exists()


@pytest.mark.django_db
def test_accepted_link_hosts(staff_client: APIClient) -> None:
    SiteSettingsFactory.create()
    for url in ("https://t.me/glowderma", "https://telegram.me/glowderma", "https://T.ME/GlowDerma"):
        response = staff_client.patch(URL, {"telegram_url": url}, format="json")
        assert response.status_code == 200, (url, response.data)
    for url in ("https://instagram.com/glowderma", "https://www.instagram.com/glowderma?igsh=1"):
        response = staff_client.patch(URL, {"instagram_url": url}, format="json")
        assert response.status_code == 200, (url, response.data)
    response = staff_client.patch(URL, {"website_url": "https://www.glowderma.uz:443/uz/"}, format="json")
    assert response.status_code == 200, response.data


# --- legal texts ------------------------------------------------------------------------------------------


@pytest.mark.django_db
def test_patch_texts_are_trimmed_audited_as_digests_and_never_publish(
    staff_client: APIClient, django_capture_on_commit_callbacks: Any
) -> None:
    SiteSettingsFactory.create()
    terms_sent = "  1. Umumiy qoidalar.\r\nGlow Derma ilovasidan foydalanish shartlari.\n\n "
    with django_capture_on_commit_callbacks(execute=True):
        response = staff_client.patch(
            URL,
            {
                "terms": {"uz": terms_sent, "ru": "Условия"},
                "privacy_policy": {"uz": PRIVACY_UZ},
                "booking_policy": {"uz": "Qabulni 2 soat oldin bekor qiling."},
            },
            format="json",
        )
    assert response.status_code == 200, response.data
    assert response.data["terms"] == {"uz": TERMS_UZ, "ru": "Условия", "en": ""}
    assert response.data["privacy_policy"] == {"uz": PRIVACY_UZ, "ru": "", "en": ""}
    assert response.data["booking_policy"]["uz"] == "Qabulni 2 soat oldin bekor qiling."
    # editing never publishes: no version, and the app still sees unpublished terms
    assert (response.data["terms_version"], response.data["terms_updated_at"]) == (None, None)
    public = APIClient().get(PUBLIC, HTTP_ACCEPT_LANGUAGE="ru").json()
    assert public["terms"] == "Условия" and public["terms_version"] is None
    entry = AuditEntry.objects.get(action="settings.updated")
    assert entry.after == {
        "terms": {
            "uz": {"changed": True, "old_len": 0, "new_len": len(TERMS_UZ), "sha256": sha(TERMS_UZ)},
            "ru": {"changed": True, "old_len": 0, "new_len": 7, "sha256": sha("Условия")},
        },
        "privacy_policy": {
            "uz": {"changed": True, "old_len": 0, "new_len": len(PRIVACY_UZ), "sha256": sha(PRIVACY_UZ)}
        },
        "booking_policy": {
            "uz": {
                "changed": True,
                "old_len": 0,
                "new_len": len("Qabulni 2 soat oldin bekor qiling."),
                "sha256": sha("Qabulni 2 soat oldin bekor qiling."),
            }
        },
    }
    assert "Umumiy" not in json.dumps(entry.after, ensure_ascii=False)  # never the text itself
    # emptying a text: digest null; the same text again changes nothing
    cleared = staff_client.patch(URL, {"terms": {"ru": "", "uz": TERMS_UZ}}, format="json")
    assert cleared.status_code == 200, cleared.data
    assert AuditEntry.objects.filter(action="settings.updated").latest("id").after == {
        "terms": {"ru": {"changed": True, "old_len": 7, "new_len": 0, "sha256": None}}
    }
    assert AuditEntry.objects.filter(action="settings.updated").count() == 2


@pytest.mark.django_db
def test_published_uz_texts_cannot_be_emptied(staff_client: APIClient, pinned: list[datetime]) -> None:
    fill_texts()
    assert publish(staff_client).status_code == 200
    message = "E’lon qilingan matnning o‘zbekcha nusxasini bo‘sh qoldirib bo‘lmaydi."
    response = staff_client.patch(URL, {"terms": {"uz": "  "}, "privacy_policy": {"uz": ""}}, format="json")
    assert response.status_code == 400
    assert errors(response) == [("terms.uz", "blank", message), ("privacy_policy.uz", "blank", message)]
    row = SiteSettings.objects.get(pk=1)
    assert (row.terms_uz, row.privacy_policy_uz) == (TERMS_UZ, PRIVACY_UZ)
    # the other languages and the booking policy may be emptied; the uz texts may change
    response = staff_client.patch(
        URL, {"terms": {"ru": "", "uz": TERMS_UZ + " 2"}, "booking_policy": {"uz": ""}}, format="json"
    )
    assert response.status_code == 200, response.data


# --- publish ----------------------------------------------------------------------------------------------


@pytest.mark.django_db
def test_publish_requires_an_idempotency_key(staff_client: APIClient) -> None:
    fill_texts()
    response = staff_client.post(PUBLISH, format="json")
    assert response.status_code == 400
    assert response.data["code"] == "idempotency_key_required"
    assert SiteSettings.objects.get(pk=1).terms_version == ""


@pytest.mark.django_db
@pytest.mark.parametrize(
    "texts",
    [
        {"terms_uz": "", "privacy_policy_uz": ""},
        {"terms_uz": "   \n "},
        {"privacy_policy_uz": ""},
        {"terms_uz": "", "terms_ru": "Условия", "terms_en": "Terms"},
    ],
)
def test_publish_refuses_without_the_uz_texts(staff_client: APIClient, texts: dict[str, str]) -> None:
    fill_texts(**texts)
    response = publish(staff_client)
    assert response.status_code == 409
    assert response.data == {
        "code": "conflict",
        "message": "Foydalanish shartlari va maxfiylik siyosatining o‘zbekcha matnini kiriting, so‘ng "
        "e’lon qiling.",
        "errors": [],
        "request_id": response.data["request_id"],
    }
    row = SiteSettings.objects.get(pk=1)
    assert (row.terms_version, row.terms_updated_at) == ("", None)
    assert not AuditEntry.objects.filter(action="settings.terms_published").exists()


@pytest.mark.django_db
def test_publish_versions_by_tashkent_day_audits_and_invalidates_the_public_cache(
    staff_client: APIClient,
    staff: StaffUser,
    pinned: list[datetime],
    django_capture_on_commit_callbacks: Any,
) -> None:
    fill_texts(terms_ru="Условия")
    assert APIClient().get(PUBLIC).json()["terms_version"] is None  # cached as unpublished
    with django_capture_on_commit_callbacks(execute=True):
        first = publish(staff_client)
    assert first.status_code == 200, first.data
    assert first.data["terms_version"] == "20261020-1"
    assert first.data["terms_updated_at"] == "2026-10-20T10:00:00+05:00"
    assert first.data["updated_by"] == {"id": staff.pk, "display_name": staff.full_name}
    public = APIClient().get(PUBLIC).json()
    assert public["terms_version"] == "20261020-1"
    assert public["terms_updated_at"] == "2026-10-20T10:00:00+05:00"
    entry = AuditEntry.objects.get(action="settings.terms_published")
    assert (entry.entity_type, entry.entity_id, entry.actor_id) == ("settings", "1", staff.pk)
    assert entry.after == {
        "from": None,
        "to": "20261020-1",
        "sha256": {
            "terms": {"uz": sha(TERMS_UZ), "ru": sha("Условия"), "en": None},
            "privacy_policy": {"uz": sha(PRIVACY_UZ), "ru": None, "en": None},
        },
    }
    # the same day again: n grows
    pinned.append(at(2026, 10, 20, 23, 59))
    second = publish(staff_client)
    assert second.data["terms_version"] == "20261020-2"
    # 21:30 UTC on the 20th is the 21st in Tashkent
    pinned.append(datetime.fromisoformat("2026-10-20T21:30:00+00:00"))
    third = publish(staff_client)
    assert third.data["terms_version"] == "20261021-1"
    assert third.data["terms_updated_at"] == "2026-10-21T02:30:00+05:00"
    entries = AuditEntry.objects.filter(action="settings.terms_published")
    log = [cast(dict[str, Any], entry.after) for entry in entries]
    assert sorted((data["from"] or "", data["to"]) for data in log) == [
        ("", "20261020-1"),
        ("20261020-1", "20261020-2"),
        ("20261020-2", "20261021-1"),
    ]


@pytest.mark.django_db
def test_publish_replays_with_the_same_key(staff_client: APIClient, pinned: list[datetime]) -> None:
    fill_texts()
    key = str(uuid.uuid4())
    first = publish(staff_client, key)
    pinned.append(at(2026, 10, 20, 11, 0))
    second = publish(staff_client, key)
    assert first.status_code == second.status_code == 200
    assert second["Idempotent-Replayed"] == "true" and second.data == first.data
    assert SiteSettings.objects.get(pk=1).terms_version == "20261020-1"
    assert AuditEntry.objects.filter(action="settings.terms_published").count() == 1


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


@pytest.mark.django_db(transaction=True)
def test_concurrent_publications_get_distinct_versions(staff: StaffUser, pinned: list[datetime]) -> None:
    fill_texts()
    go = threading.Event()
    versions: list[str] = []
    failures: list[BaseException] = []
    lock = threading.Lock()

    def run() -> None:
        try:
            go.wait(10)
            row = services.publish_terms(actor=staff)
            with lock:
                versions.append(row.terms_version)
        except BaseException as exc:  # noqa: BLE001 - reported by the assertion below
            with lock:
                failures.append(exc)
        finally:
            connection.close()

    threads = [threading.Thread(target=run) for _ in range(3)]
    for thread in threads:
        thread.start()
    go.set()
    for thread in threads:
        thread.join(timeout=10)
    assert not failures
    assert sorted(versions) == ["20261020-1", "20261020-2", "20261020-3"]
    assert SiteSettings.objects.get(pk=1).terms_version == "20261020-3"
    assert AuditEntry.objects.filter(action="settings.terms_published").count() == 3


@pytest.mark.django_db
def test_public_settings_serve_the_contacts_and_texts(
    staff_client: APIClient, pinned: list[datetime], django_capture_on_commit_callbacks: Any
) -> None:
    """Customer contract §2.4: the values set through the admin API reach ``settings/public/``."""
    with django_capture_on_commit_callbacks(execute=True):
        response = staff_client.patch(
            URL,
            {
                "store_phone": "+998712000001",
                "cosmetologist_phone": "+998901112233",
                "telegram_url": "https://t.me/glowderma",
                "instagram_url": "https://instagram.com/glowderma",
                "website_url": "https://glowderma.uz",
                "terms": {"uz": TERMS_UZ, "en": "Terms of use"},
                "privacy_policy": {"uz": PRIVACY_UZ},
                "booking_policy": {"uz": "Bekor qilish bepul.", "ru": "Отмена бесплатна."},
            },
            format="json",
        )
        assert response.status_code == 200, response.data
        assert publish(staff_client).status_code == 200
    selectors.invalidate_public_settings()
    en = APIClient().get(PUBLIC, HTTP_ACCEPT_LANGUAGE="en").json()
    ru = APIClient().get(PUBLIC, HTTP_ACCEPT_LANGUAGE="ru").json()
    contacts = ("store_phone", "cosmetologist_phone", "telegram_url", "instagram_url")
    assert {key: en[key] for key in contacts} == {
        "store_phone": "+998712000001",
        "cosmetologist_phone": "+998901112233",
        "telegram_url": "https://t.me/glowderma",
        "instagram_url": "https://instagram.com/glowderma",
    }
    assert en["website_url"] == "https://glowderma.uz"
    assert (en["terms"], en["privacy_policy"], en["booking_policy"]) == (
        "Terms of use",
        PRIVACY_UZ,
        "Bekor qilish bepul.",
    )  # en missing → uz
    assert (ru["terms"], ru["booking_policy"]) == (TERMS_UZ, "Отмена бесплатна.")
    assert en["terms_version"] == ru["terms_version"] == "20261020-1"
