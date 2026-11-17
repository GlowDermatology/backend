"""The audit log export in Russian and English (feat/admin-ux, DESIGN §G): the ``N_``-marked file name and
the header row follow the request's language; the values (action codes, ids) and the ``export.downloaded``
audit row stay as they are."""

from __future__ import annotations

import json
import re

import pytest
from rest_framework.test import APIClient

from glowderma.accounts.tests.languages import overlay
from glowderma.core.models import AuditEntry

pytestmark = pytest.mark.django_db

URL = "/api/v1/admin/audit-log/"
TRANSLATIONS: dict[str, dict[str, str]] = {
    "harakatlar-jurnali": {"en": "audit-log", "ru": "журнал-действий"},
    "ID": {"en": "ID", "ru": "ID"},
    "Sana": {"en": "Date", "ru": "Дата"},
    "Xodim": {"en": "Staff member", "ru": "Сотрудник"},
    "Amal": {"en": "Action", "ru": "Действие"},
    "Obyekt": {"en": "Object", "ru": "Объект"},
    "Obyekt ID": {"en": "Object ID", "ru": "ID объекта"},
    "IP": {"en": "IP", "ru": "IP"},
    "So‘rov ID": {"en": "Request ID", "ru": "ID запроса"},
}


@pytest.fixture
def translations(monkeypatch: pytest.MonkeyPatch) -> None:
    overlay(monkeypatch, TRANSLATIONS)


def test_the_audit_log_export_follows_the_language(translations: None, staff_client: APIClient) -> None:
    english = staff_client.get(URL, {"export": "csv"}, HTTP_ACCEPT_LANGUAGE="en")
    assert english.status_code == 200 and english["Content-Language"] == "en"
    assert re.fullmatch(r'attachment; filename="audit-log-\d{8}-\d{4}\.csv"', english["Content-Disposition"])
    assert english.content.decode("utf-8-sig").split("\r\n")[0] == (
        '"ID";"Date";"Staff member";"Action";"Object";"Object ID";"IP";"Request ID"'
    )
    russian = staff_client.get(URL, {"export": "csv"}, HTTP_ACCEPT_LANGUAGE="ru")
    disposition = russian["Content-Disposition"]
    assert disposition.startswith('attachment; filename="zhurnal-deystviy-') and (
        "filename*=UTF-8''%D0%B6%D1%83%D1%80%D0%BD%D0%B0%D0%BB-" in disposition
    ), disposition
    uzbek = staff_client.get(URL, {"export": "csv"})
    assert re.fullmatch(
        r'attachment; filename="harakatlar-jurnali-\d{8}-\d{4}\.csv"', uzbek["Content-Disposition"]
    )
    lines = uzbek.content.decode("utf-8-sig").split("\r\n")
    assert lines[0] == '"ID";"Sana";"Xodim";"Amal";"Obyekt";"Obyekt ID";"IP";"So‘rov ID"'
    # the earlier downloads are listed with their stored (code) values, never translated
    assert '"export.downloaded"' in lines[1]
    stored = json.dumps(
        list(AuditEntry.objects.filter(action="export.downloaded").values()), default=str, ensure_ascii=False
    )
    assert AuditEntry.objects.filter(action="export.downloaded").count() == 3
    assert not re.search("[А-Яа-яЁё]", stored) and "Staff member" not in stored
