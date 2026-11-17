"""Contract §2.13: the three audit tiers of ``GET audit-log/`` and its export, ``request_id``, ``facets/``.

Ordinary staff see business entries and their own sign-ins and exports, never an IP; managers add staff
management and every staff sign-in, with IPs; superusers see everything (``core.audit_scopes``).
"""

from datetime import datetime
from typing import Any

import pytest
from django.conf import settings
from rest_framework.test import APIClient

from glowderma.accounts import tokens
from glowderma.accounts.models import StaffUser
from glowderma.accounts.tests.factories import AuthSessionFactory, StaffUserFactory
from glowderma.core import audit, audit_scopes, seeding
from glowderma.core.models import AuditEntry
from glowderma.core.timeutils import TASHKENT

pytestmark = pytest.mark.django_db

URL = "/api/v1/admin/audit-log/"
FACETS = "/api/v1/admin/audit-log/facets/"
AT = datetime(2026, 11, 1, 10, tzinfo=TASHKENT)


class FakeRequest:
    def __init__(self, request_id: str = "6f1c2a0e9b8d4c37", ip: str = "203.0.113.7") -> None:
        self.META = {"REMOTE_ADDR": ip, "HTTP_USER_AGENT": "pytest-agent"}
        self.client_ip = ip
        self.request_id = request_id


def client_for(user: StaffUser) -> APIClient:
    access, _ = tokens.issue_access(AuthSessionFactory.create(staff=user))
    client = APIClient()
    client.default_format = "json"
    client.credentials(
        HTTP_AUTHORIZATION=f"Bearer {access}", HTTP_X_GD_INTERNAL_SECRET=settings.INTERNAL_API_SECRET
    )
    return client


def entry(**kwargs: Any) -> AuditEntry:
    with seeding.pinned_clock(AT):
        return audit.record(**kwargs)


@pytest.fixture
def people() -> dict[str, StaffUser]:
    return {
        "me": StaffUserFactory.create(full_name="Oddiy Xodim"),
        "other": StaffUserFactory.create(full_name="Boshqa Xodim"),
        "manager": StaffUserFactory.create(full_name="Menejer", is_manager=True),
        "root": StaffUserFactory.create(full_name="Super", is_superuser=True),
    }


@pytest.fixture
def journal(people: dict[str, StaffUser]) -> dict[str, AuditEntry]:
    me, other, manager = people["me"], people["other"], people["manager"]
    return {
        "business": entry(
            action="cashback.adjusted",
            entity_type="customer",
            entity_id="5",
            actor=other,
            request=FakeRequest("aaaa1111-business"),
            after={"amount": 2950, "ip": "203.0.113.9", "nested": {"user_agent": "Mozilla"}},
        ),
        "my_login": entry(
            action="auth.login_succeeded",
            entity_type="staff",
            entity_id=str(me.pk),
            actor=me,
            request=FakeRequest("bbbb2222-mylogin"),
            after={"method": "password", "location": "Tashkent, UZ"},
        ),
        "my_failed": entry(
            action="auth.login_failed", entity_type="staff", entity_id=str(me.pk), after={"email": me.email}
        ),
        "other_login": entry(
            action="auth.login_succeeded",
            entity_type="staff",
            entity_id=str(other.pk),
            actor=other,
            request=FakeRequest("cccc3333-otherlogin"),
        ),
        "my_export": entry(
            action="export.downloaded",
            entity_type="orders",
            entity_id="orders",
            actor=me,
            after={"row_count": 1},
        ),
        "other_export": entry(
            action="export.downloaded", entity_type="orders", entity_id="orders", actor=other
        ),
        "staff_admin": entry(
            action="staff.created",
            entity_type="staff",
            entity_id=str(other.pk),
            actor=manager,
            after={"email": other.email},
        ),
        "my_staff_change": entry(
            action="staff.updated", entity_type="staff", entity_id=str(me.pk), actor=manager
        ),
        "customer_auth": entry(
            action="auth.device_mismatch", entity_type="customer", entity_id="3", actor_type="customer"
        ),
        "system": entry(
            action="outbox.dead",
            entity_type="outbox",
            entity_id="9",
            request=FakeRequest("dddd4444-system"),
        ),
        "by_hand": entry(action="billz.sale_recorded_by_hand", entity_type="order", entity_id="1", actor=me),
        "console": entry(action="console.login_failed", entity_type="staff", entity_id=str(me.pk)),
        "unknown": entry(action="future.feature", entity_type="x", entity_id="1", actor=me),
    }


def ids(client: APIClient, **params: Any) -> set[int]:
    response = client.get(URL, {"page_size": 50, **params})
    assert response.status_code == 200, response.data
    return {row["id"] for row in response.data["results"]}


def keys(journal: dict[str, AuditEntry], *names: str) -> set[int]:
    return {journal[name].pk for name in names}


def test_ordinary_staff_see_business_and_their_own_security_entries(
    people: dict[str, StaffUser], journal: dict[str, AuditEntry]
) -> None:
    client = client_for(people["me"])
    assert ids(client) == keys(journal, "business", "my_login", "my_failed", "my_export", "customer_auth")
    rows = {row["id"]: row for row in client.get(URL, {"page_size": 50}).data["results"]}
    business = rows[journal["business"].pk]
    assert business["ip"] is None  # never an IP for ordinary staff
    assert business["data"] == {"amount": 2950, "ip": None, "nested": {"user_agent": None}}
    assert business["request_id"] == "aaaa1111-business"  # the request id stays (support handle)
    assert rows[journal["my_login"].pk]["data"] == {"method": "password", "location": None}
    # stored rows are untouched
    stored = AuditEntry.objects.get(pk=journal["business"].pk).after
    assert isinstance(stored, dict) and stored["ip"] == "203.0.113.9"


def test_hidden_filters_answer_an_empty_list_not_403(
    people: dict[str, StaffUser], journal: dict[str, AuditEntry]
) -> None:
    client = client_for(people["me"])
    assert ids(client, action="outbox.dead") == set()
    assert ids(client, action="staff.updated") == set()  # even about oneself: staff management is manager+
    assert ids(client, action="auth.login_succeeded") == keys(journal, "my_login")
    assert ids(client, actor=people["other"].pk) == keys(journal, "business")
    assert ids(client, object_type="outbox") == set()
    assert ids(client, object_type="staff", object_id=str(people["other"].pk)) == set()
    assert ids(client, request_id="dddd4444") == set()
    assert ids(client, request_id="cccc3333") == set()
    assert ids(client, action="future.feature") == set()  # unclassified: fail closed


def test_managers_add_staff_management_all_sign_ins_and_ips(
    people: dict[str, StaffUser], journal: dict[str, AuditEntry]
) -> None:
    client = client_for(people["manager"])
    hidden = keys(journal, "system", "by_hand", "console", "unknown")
    assert ids(client) == {row.pk for row in journal.values()} - hidden
    row = next(r for r in client.get(URL).data["results"] if r["id"] == journal["business"].pk)
    assert row["ip"] == "203.0.113.7"
    assert row["data"]["ip"] == "203.0.113.9" and row["data"]["nested"] == {"user_agent": "Mozilla"}
    assert ids(client, action="billz.sale_recorded_by_hand") == set()


def test_superusers_see_everything(people: dict[str, StaffUser], journal: dict[str, AuditEntry]) -> None:
    client = client_for(people["root"])
    assert ids(client) == {row.pk for row in journal.values()}
    assert ids(client, action="console.login_failed") == keys(journal, "console")


def test_tier_follows_the_flags_on_the_next_request(
    people: dict[str, StaffUser], journal: dict[str, AuditEntry]
) -> None:
    manager = people["manager"]
    client = client_for(manager)
    assert journal["staff_admin"].pk in ids(client)
    manager.is_manager = False
    manager.save(update_fields=["is_manager"])
    assert journal["staff_admin"].pk not in ids(client)
    assert client.get(FACETS).data["tier"] == "staff"


def test_request_id_prefix_filter(people: dict[str, StaffUser], journal: dict[str, AuditEntry]) -> None:
    root = client_for(people["root"])
    assert ids(root, request_id="bbbb2222") == keys(journal, "my_login")
    assert ids(root, request_id="bbbb2222-mylogin") == keys(journal, "my_login")
    assert ids(root, request_id="BBBB2222") == set()  # case-sensitive, like the stored ids
    assert ids(root, request_id="2222-myl") == set()  # a prefix, not a substring
    assert ids(root, request_id="dddd4444") == keys(journal, "system")
    # LIKE wildcards cannot sneak in: they are not in the allowed alphabet
    for value in ("bbbb%", "bbbb_222", "short", "x" * 65, "bbbb 2222", "ббббб2222", "bbbb2222\nx"):
        response = root.get(URL, {"request_id": value})
        assert response.status_code == 400, value
        assert [(e["field"], e["code"]) for e in response.data["errors"]] == [("request_id", "invalid")]
    assert root.get(URL, {"request_id": "bbbb2222\u0000"}).status_code == 400  # DRF's NUL guard
    me = client_for(people["me"])
    assert ids(me, request_id="bbbb2222") == keys(journal, "my_login")


def test_export_applies_the_tier_and_blanks_ips(
    people: dict[str, StaffUser], journal: dict[str, AuditEntry]
) -> None:
    client = client_for(people["me"])
    response = client.get(URL, {"export": "csv", "ordering": "at"})
    assert response.status_code == 200
    lines = [line for line in response.content.decode("utf-8-sig").split("\r\n")[1:] if line]
    exported = {int(line.split(";")[0].strip('"')) for line in lines}
    assert exported == keys(journal, "business", "my_login", "my_failed", "my_export", "customer_auth")
    assert all(line.split(";")[6] == '""' for line in lines)  # the IP column is empty
    assert "203.0.113.7" not in response.content.decode("utf-8-sig")
    root = client_for(people["root"]).get(URL, {"export": "csv", "request_id": "aaaa1111"})
    assert '"203.0.113.7"' in root.content.decode("utf-8-sig")
    downloaded = AuditEntry.objects.filter(action="export.downloaded", actor_id=people["root"].pk).get()
    assert isinstance(downloaded.after, dict) and downloaded.after["filters"]["request_id"] == "aaaa1111"


def test_export_audit_rows_follow_their_own_tier(people: dict[str, StaffUser]) -> None:
    """An ordinary member's own ``export.downloaded`` row is visible to them; another member's is not."""
    me, other = client_for(people["me"]), client_for(people["other"])
    me.get(URL, {"export": "csv"})
    mine = AuditEntry.objects.get(action="export.downloaded", actor_id=people["me"].pk)
    assert mine.pk in ids(me)
    assert mine.pk not in ids(other)
    assert mine.pk in ids(client_for(people["manager"]))


@pytest.mark.parametrize(
    "who,tier,ip_visible,staff_visible",
    [("me", "staff", False, False), ("manager", "manager", True, True), ("root", "superuser", True, True)],
)
def test_facets(
    people: dict[str, StaffUser], who: str, tier: str, ip_visible: bool, staff_visible: bool
) -> None:
    response = client_for(people[who]).get(FACETS)
    assert response.status_code == 200
    body = response.data
    assert set(body) == {"tier", "ip_visible", "visible_prefixes", "own_only_prefixes", "hidden_actions"}
    assert body["tier"] == tier and body["ip_visible"] is ip_visible
    assert ("staff" in body["visible_prefixes"]) is staff_visible
    assert body == audit_scopes.facets(people[who])
    if who == "me":
        assert body["own_only_prefixes"] == ["auth", "export"]
        assert "billz.sale_recorded_by_hand" in body["hidden_actions"]
    if who == "root":
        assert "console" in body["visible_prefixes"] and body["hidden_actions"] == []


def test_facets_needs_a_staff_session(anon_client: APIClient) -> None:
    assert anon_client.get(FACETS).status_code == 401
