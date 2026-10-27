"""Contract §2.7 over HTTP: ``GET cashback/policy/`` (the seeded v1–v3 at the design anchor) and
``POST cashback/policy-versions/`` (validation table rows with code, message and field path, warnings,
supersession, idempotency)."""

from __future__ import annotations

import uuid
from collections.abc import Callable
from datetime import datetime
from typing import Any

import pytest
from django.conf import settings
from rest_framework.test import APIClient

from glowderma.accounts import seeds as accounts_seeds
from glowderma.accounts.models import StaffUser
from glowderma.core import seeding, timeutils
from glowderma.core.models import AuditEntry
from glowderma.core.seeding import SeedContext
from glowderma.loyalty.models import CashbackPolicy, CashbackTier
from glowderma.loyalty.seeds import seed_loyalty

pytestmark = pytest.mark.django_db

ANCHOR = datetime.fromisoformat(seeding.DEFAULT_ANCHOR)  # 2026-10-24 12:05 Tashkent
POLICY_URL = "/api/v1/admin/cashback/policy/"
VERSIONS_URL = "/api/v1/admin/cashback/policy-versions/"
MLN = 1_000_000


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> Callable[[datetime], None]:
    state = {"now": ANCHOR}
    monkeypatch.setattr(timeutils, "now", lambda: state["now"])

    def move(instant: datetime) -> None:
        state["now"] = instant

    return move


@pytest.fixture
def admin(monkeypatch: pytest.MonkeyPatch) -> StaffUser:
    monkeypatch.delenv("SEED_ADMIN_PASSWORD", raising=False)
    return accounts_seeds.ensure_admin()


@pytest.fixture
def seeded(admin: StaffUser, clock: Callable[[datetime], None]) -> StaffUser:
    """The design versions v1–v3 (03 §7.7) with the demo admin as author of v2/v3."""
    seed_loyalty(SeedContext(anchor=ANCHOR, keep_open=True))
    return admin


def client_now(staff: StaffUser) -> APIClient:
    """A staff client for a pinned clock moved past the real time (a JWT issued in the future would be
    refused by PyJWT's ``iat`` check, so the credentials are forced; the permission still sees aud=staff)."""
    client = APIClient()
    client.default_format = "json"
    client.force_authenticate(user=staff, token={"aud": "staff"})  # type: ignore[arg-type]  # claims dict
    client.credentials(HTTP_X_GD_INTERNAL_SECRET=settings.INTERNAL_API_SECRET)
    return client


def post(client: APIClient, body: dict[str, Any], key: str | None = None) -> Any:
    return client.post(VERSIONS_URL, body, format="json", HTTP_IDEMPOTENCY_KEY=key or str(uuid.uuid4()))


def tier(start: int, end: int | None, percent: Any) -> dict[str, Any]:
    return {"from": start, "to": end, "percent": percent}


def body(*tiers: dict[str, Any], effective_from: str = "2026-11-01T00:00:00+05:00") -> dict[str, Any]:
    return {
        "effective_from": effective_from,
        "basis": "before_purchase",
        "earn_on_cashback_paid": False,
        "tiers": list(tiers),
    }


def errors(response: Any) -> list[tuple[str | None, str, str]]:
    return [(e["field"], e["code"], e["message"]) for e in response.data["errors"]]


SEED_TIERS = [tier(0, 20 * MLN, "1.00"), tier(20 * MLN, 30 * MLN, "2.00"), tier(30 * MLN, 50 * MLN, "3.00")]


# --- GET cashback/policy/ -----------------------------------------------------------------------------------


def test_get_policy_reproduces_the_seeded_design(staff_client: APIClient, seeded: StaffUser) -> None:
    response = staff_client.get(POLICY_URL)
    assert response.status_code == 200, response.data
    data = response.json()
    author = {"id": seeded.pk, "display_name": "Administrator"}
    tier_ids = {
        (t.policy.version, t.from_amount): t.pk for t in CashbackTier.objects.select_related("policy")
    }

    def tiers(version: int, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [{"id": tier_ids[(version, row["from"])], **row} for row in rows]

    v3 = {
        "version": 3,
        "state": "active",
        "effective_from": "2026-10-01T00:00:00+05:00",
        "effective_to": None,
        "basis": "before_purchase",
        "earn_on_cashback_paid": True,
        "tiers": tiers(3, SEED_TIERS),
        "author": author,
        "created_at": "2026-09-20T10:00:00+05:00",
        "superseded_by": None,
    }
    v2 = {
        "version": 2,
        "state": "expired",
        "effective_from": "2026-08-15T00:00:00+05:00",
        "effective_to": "2026-10-01T00:00:00+05:00",
        "basis": "before_purchase",
        "earn_on_cashback_paid": False,
        "tiers": tiers(2, SEED_TIERS[:2]),
        "author": author,
        "created_at": "2026-08-01T10:00:00+05:00",
        "superseded_by": None,
    }
    v1 = {
        "version": 1,
        "state": "expired",
        "effective_from": "2026-06-01T00:00:00+05:00",
        "effective_to": "2026-08-15T00:00:00+05:00",
        "basis": "before_purchase",
        "earn_on_cashback_paid": False,
        "tiers": tiers(1, SEED_TIERS[:1]),
        "author": None,  # system / seed: the FE renders "Tizim"
        "created_at": "2026-05-20T10:00:00+05:00",
        "superseded_by": None,
    }
    assert data == {
        "active": v3,
        "scheduled": None,
        "versions": [v3, v2, v1],
        "next_effective_from": "2026-11-01T00:00:00+05:00",
    }
    # §5.4 summaries: each version adds exactly one band to the previous non-superseded one
    # ("Boshlang‘ich: 20 mln gacha 1%", "20–30 mln: 2% qo‘shildi", "30–50 mln: 3% qo‘shildi").
    keyed = {v["version"]: {(t["from"], t["to"], t["percent"]) for t in v["tiers"]} for v in data["versions"]}
    assert keyed[1] == {(0, 20 * MLN, "1.00")}
    assert keyed[2] - keyed[1] == {(20 * MLN, 30 * MLN, "2.00")} and not keyed[1] - keyed[2]
    assert keyed[3] - keyed[2] == {(30 * MLN, 50 * MLN, "3.00")} and not keyed[2] - keyed[3]


def test_get_policy_runs_a_bounded_number_of_queries(
    staff_client: APIClient, seeded: StaffUser, django_assert_max_num_queries: Any
) -> None:
    staff_client.get(POLICY_URL)  # warm the session/user lookups the same way
    with django_assert_max_num_queries(6):  # auth (session, staff) + versions + tiers
        assert staff_client.get(POLICY_URL).status_code == 200


def test_get_policy_without_any_version(staff_client: APIClient, clock: Callable[[datetime], None]) -> None:
    response = staff_client.get(POLICY_URL)
    assert response.json() == {
        "active": None,
        "scheduled": None,
        "versions": [],
        "next_effective_from": "2026-11-01T00:00:00+05:00",
    }


def test_next_effective_from_follows_the_tashkent_calendar_month(
    staff: StaffUser, clock: Callable[[datetime], None]
) -> None:
    # 31 Dec 2026 20:00 UTC is already 1 Jan 2027 01:00 in Tashkent → the next month start is 1 Feb 2027
    clock(datetime.fromisoformat("2026-12-31T20:00:00+00:00"))
    assert client_now(staff).get(POLICY_URL).json()["next_effective_from"] == "2027-02-01T00:00:00+05:00"


# --- POST cashback/policy-versions/ -------------------------------------------------------------------------


def test_create_returns_the_scheduled_version_and_warnings(
    staff_client: APIClient, seeded: StaffUser
) -> None:
    response = post(
        staff_client,
        body(
            tier(30 * MLN, 50 * MLN, "3"),  # submitted unsorted; rendered sorted by from
            tier(0, 20 * MLN, "1.5"),
            tier(20 * MLN, 30 * MLN, "2.30"),
            tier(60 * MLN, 80 * MLN, "0.57"),
        ),
    )
    assert response.status_code == 201, response.data
    data = response.json()
    version = data["version"]
    assert version["version"] == 4 and version["state"] == "scheduled"
    assert version["effective_from"] == "2026-11-01T00:00:00+05:00" and version["effective_to"] is None
    assert version["superseded_by"] is None
    assert version["created_at"] == "2026-10-24T12:05:00+05:00"
    assert [(t["from"], t["to"], t["percent"]) for t in version["tiers"]] == [
        (0, 20 * MLN, "1.50"),
        (20 * MLN, 30 * MLN, "2.30"),
        (30 * MLN, 50 * MLN, "3.00"),
        (60 * MLN, 80 * MLN, "0.57"),
    ]
    assert version["author"]["id"] == response.wsgi_request.user.pk
    assert data["superseded_version"] is None
    assert data["warnings"] == [
        {"code": "gap", "from": 50 * MLN, "to": 60 * MLN},
        {"code": "uncovered_above", "from": 80 * MLN, "to": None},
    ]
    created = AuditEntry.objects.get(action="cashback.policy_version_created")
    assert created.entity_id == "4" and created.actor_id == response.wsgi_request.user.pk
    # the new version is the scheduled one; v3 stays active until it starts
    policy = staff_client.get(POLICY_URL).json()
    assert policy["active"]["version"] == 3 and policy["scheduled"]["version"] == 4
    assert policy["active"]["effective_to"] == "2026-11-01T00:00:00+05:00"


def test_a_new_version_supersedes_the_scheduled_one(staff_client: APIClient, seeded: StaffUser) -> None:
    first = post(staff_client, body(tier(0, None, "1.00")))
    assert first.status_code == 201 and first.json()["warnings"] == []
    second = post(staff_client, body(tier(0, None, "2.00"), effective_from="2026-12-01T00:00:00+05:00"))
    assert second.status_code == 201, second.data
    assert second.json()["version"]["version"] == 5 and second.json()["superseded_version"] == 4
    superseded = AuditEntry.objects.get(action="cashback.policy_version_superseded")
    assert superseded.entity_id == "4" and superseded.after == {"state": "superseded", "superseded_by": 5}
    policy = staff_client.get(POLICY_URL).json()
    assert [(v["version"], v["state"], v["superseded_by"]) for v in policy["versions"]] == [
        (5, "scheduled", None),
        (4, "superseded", 5),
        (3, "active", None),
        (2, "expired", None),
        (1, "expired", None),
    ]
    assert policy["scheduled"]["version"] == 5
    v3 = policy["versions"][2]
    assert v3["effective_to"] == "2026-12-01T00:00:00+05:00"  # the superseded v4 never takes effect


def test_state_follows_time(
    staff_client: APIClient, staff: StaffUser, seeded: StaffUser, clock: Callable[[datetime], None]
) -> None:
    assert post(staff_client, body(*SEED_TIERS)).status_code == 201
    clock(datetime.fromisoformat("2026-11-01T00:00:00+05:00"))
    policy = client_now(staff).get(POLICY_URL).json()
    assert policy["active"]["version"] == 4 and policy["scheduled"] is None
    assert [v["state"] for v in policy["versions"]] == ["active", "expired", "expired", "expired"]
    assert policy["next_effective_from"] == "2026-12-01T00:00:00+05:00"


def test_create_replays_and_refuses_a_reused_key(staff_client: APIClient, seeded: StaffUser) -> None:
    key = str(uuid.uuid4())
    payload = body(tier(0, None, "1.00"))
    first = post(staff_client, payload, key)
    replay = post(staff_client, payload, key)
    assert first.status_code == replay.status_code == 201
    assert replay.json() == first.json() and replay["Idempotent-Replayed"] == "true"
    assert CashbackPolicy.objects.count() == 4
    assert AuditEntry.objects.filter(action="cashback.policy_version_created").count() == 1
    reused = post(staff_client, body(tier(0, None, "2.00")), key)
    assert reused.status_code == 422 and reused.data["code"] == "idempotency_key_reused"


def test_create_requires_an_idempotency_key(staff_client: APIClient, seeded: StaffUser) -> None:
    response = staff_client.post(VERSIONS_URL, body(tier(0, None, "1.00")), format="json")
    assert response.status_code == 400 and response.data["code"] == "idempotency_key_required"


OVERLAP = "Darajalar ustma-ust tushmoqda."
EMPTY_RANGE = "Yuqori chegara quyi chegaradan katta bo‘lsin."
OUT_OF_RANGE = "Foiz 0 dan katta va 100 dan oshmasligi kerak."
FIELDS = "Maydonlarni tekshiring."


@pytest.mark.parametrize(
    ("payload", "message", "expected"),
    [
        pytest.param(
            body(),
            "Kamida bitta daraja kerak.",
            [("tiers", "min_items", "Kamida bitta daraja kerak.")],
            id="min_items",
        ),
        pytest.param(
            body(tier(0, 10 * MLN, "1"), tier(10 * MLN, 10 * MLN, "2")),
            OVERLAP,
            [("tiers.1.to", "empty_range", EMPTY_RANGE)],
            id="empty_range",
        ),
        pytest.param(
            body(tier(5 * MLN, 1 * MLN, "2"), tier(20 * MLN, None, "3")),
            OVERLAP,
            [("tiers.0.to", "empty_range", EMPTY_RANGE)],
            id="to_below_from",
        ),
        pytest.param(
            # sorted: [0, 30M) (submitted #1) then [20M, ∞) (submitted #0) → the overlap names index 0
            body(tier(20 * MLN, None, "2"), tier(0, 30 * MLN, "1")),
            OVERLAP,
            [("tiers.0.from", "overlap", OVERLAP)],
            id="overlap_submitted_index",
        ),
        pytest.param(
            # an open-ended band below another one overlaps it
            body(tier(0, None, "1"), tier(20 * MLN, None, "2")),
            OVERLAP,
            [("tiers.1.from", "overlap", OVERLAP)],
            id="overlap_open_ended",
        ),
        pytest.param(
            body(tier(0, 10 * MLN, "0"), tier(10 * MLN, None, "100.01")),
            OUT_OF_RANGE,
            [
                ("tiers.0.percent", "out_of_range", OUT_OF_RANGE),
                ("tiers.1.percent", "out_of_range", OUT_OF_RANGE),
            ],
            id="out_of_range",
        ),
        pytest.param(
            body(tier(0, None, "-1")),
            OUT_OF_RANGE,
            [("tiers.0.percent", "out_of_range", OUT_OF_RANGE)],
            id="negative_percent",
        ),
        pytest.param(
            body(tier(0, None, "1.555")),
            FIELDS,
            [("tiers.0.percent", "max_decimal_places", "Ko‘pi bilan 2 ta kasr raqam.")],
            id="max_decimal_places",
        ),
        pytest.param(
            body(tier(0, None, "1.00"), effective_from="2026-10-24T12:05:00+05:00"),
            FIELDS,
            [("effective_from", "effective_from_in_past", "Kelajakdagi sanani tanlang.")],
            id="effective_from_now",
        ),
        pytest.param(
            body(tier(0, None, "1.00"), effective_from="2026-10-01T00:00:00+05:00"),
            FIELDS,
            [("effective_from", "effective_from_in_past", "Kelajakdagi sanani tanlang.")],
            id="effective_from_in_past",
        ),
        pytest.param(
            # several rules at once: one entry per (field, code) in rule order, the first rule's message
            body(
                tier(10 * MLN, 5 * MLN, "150"), tier(0, 20 * MLN, "1.555"), effective_from="2026-01-01T00:00Z"
            ),
            OVERLAP,
            [
                ("tiers.0.to", "empty_range", EMPTY_RANGE),
                ("tiers.0.from", "overlap", OVERLAP),
                ("tiers.0.percent", "out_of_range", OUT_OF_RANGE),
                ("tiers.1.percent", "max_decimal_places", "Ko‘pi bilan 2 ta kasr raqam."),
                ("effective_from", "effective_from_in_past", "Kelajakdagi sanani tanlang."),
            ],
            id="combined",
        ),
    ],
)
def test_create_validation_table(
    staff_client: APIClient,
    seeded: StaffUser,
    payload: dict[str, Any],
    message: str,
    expected: list[tuple[str, str, str]],
) -> None:
    response = post(staff_client, payload)
    assert response.status_code == 400, response.data
    assert response.data["code"] == "validation_error"
    assert response.data["message"] == message
    assert errors(response) == expected
    assert CashbackPolicy.objects.count() == 3
    assert not AuditEntry.objects.filter(action__startswith="cashback.policy").exists()


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        pytest.param(body(tier(0, None, 1.5)), [("tiers.0.percent", "invalid")], id="percent_json_number"),
        pytest.param(body(tier(0, None, "abc")), [("tiers.0.percent", "invalid")], id="percent_not_decimal"),
        pytest.param(body(tier(-1, None, "1")), [("tiers.0.from", "min_value")], id="from_negative"),
        pytest.param(
            body(tier(0, None, "1"), effective_from="2026-11-01T00:00:00"),
            [("effective_from", "invalid")],
            id="naive_datetime",
        ),
        pytest.param({**body(), "tiers": "0-20"}, [("tiers", "invalid")], id="tiers_not_a_list"),
        pytest.param({**body(), "tiers": {"from": 0}}, [("tiers", "invalid")], id="tiers_object"),
        pytest.param(
            {"tiers": [{"to": None}]},
            [
                ("effective_from", "required"),
                ("basis", "required"),
                ("earn_on_cashback_paid", "required"),
                ("tiers.0.from", "required"),
                ("tiers.0.percent", "required"),
            ],
            id="required",
        ),
        pytest.param(
            {**body(tier(0, None, "1")), "basis": "after"}, [("basis", "invalid_choice")], id="basis"
        ),
    ],
)
def test_create_field_errors(
    staff_client: APIClient,
    seeded: StaffUser,
    payload: dict[str, Any],
    expected: list[tuple[str, str]],
) -> None:
    response = post(staff_client, payload)
    assert response.status_code == 400, response.data
    assert response.data["message"] == FIELDS
    assert [(field, code) for field, code, _ in errors(response)] == expected
