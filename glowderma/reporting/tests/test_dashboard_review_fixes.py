"""Fixes of the console UX review on the dashboards: every number agrees with the rows its link lists, the
headline agrees with the banner, one late outbox message reads the same everywhere and the nightly report is
judged by the severity of what it found."""

from collections.abc import Iterator
from datetime import datetime, timedelta
from typing import Any

import pytest
from django.core.cache import cache
from django.test import Client, override_settings

from glowderma.accounts.tests.factories import CustomerSessionFactory, DeviceFactory
from glowderma.core import audit, seeding
from glowderma.core.models import AuditEntry, OutboxMessage
from glowderma.core.timeutils import TASHKENT
from glowderma.reporting import dashboards
from glowderma.reporting.dashboards import customers, money, ops, report

pytestmark = pytest.mark.django_db
NOW = datetime(2026, 10, 31, 12, 0, tzinfo=TASHKENT)


@pytest.fixture(autouse=True)
def _pinned() -> Iterator[None]:
    cache.clear()
    with seeding.pinned_clock(NOW):
        yield


def rows_behind(client: Client, href: str) -> int:
    """How many rows the changelist at ``href`` lists (a refused lookup would redirect with ``?e=1``)."""
    response = client.get(href)
    assert response.status_code == 200, (href, response.status_code, response.get("Location"))
    return int(response.context["cl"].result_count)


def _report(checks: dict[str, dict[str, Any]], *, at: datetime = NOW) -> AuditEntry:
    with seeding.pinned_clock(at):
        return AuditEntry.objects.create(
            action=report.REPORT_ACTION,
            entity_type="report",
            entity_id="consistency",
            actor_type="system",
            after={"checks": checks},
        )


def _tile(data: dict[str, Any], title: str) -> dict[str, Any]:
    return next(tile for tile in data["tiles"] if tile["title"] == title)


# --- customers: staff sign-in failures and open sessions ----------------------------------------------------


def test_staff_login_failures_count_the_console_refusals_too(ops_client: Client) -> None:
    for action in customers.STAFF_LOGIN_FAILURES:
        audit.record(action=action, entity_type="staff", entity_id="")
    audit.record(action="console.login_succeeded", entity_type="staff", entity_id="")
    with seeding.pinned_clock(NOW - timedelta(days=2)):
        audit.record(action="console.otp_failed", entity_type="staff", entity_id="")  # outside the window
    tile = _tile(customers.build(NOW), "Xodim kirish xatolari")
    assert tile["value"] == "5"
    assert rows_behind(ops_client, tile["href"]) == 5
    assert tile["state"]["key"] == dashboards.WARN


def test_open_sessions_by_platform_link_to_exactly_those_sessions(ops_client: Client) -> None:
    android = DeviceFactory.create(platform="android")
    ios = DeviceFactory.create(platform="ios")
    for _ in range(2):
        CustomerSessionFactory.create(customer=android.customer, device=android)
    CustomerSessionFactory.create(customer=ios.customer, device=ios)
    CustomerSessionFactory.create(customer=ios.customer, device=ios, revoked_at=NOW)  # closed: not counted
    CustomerSessionFactory.create(customer=ios.customer, device=None)
    data = customers.build(NOW)
    platforms = {row["label"]: row for row in data["platforms"]}
    assert {label: row["raw"] for label, row in platforms.items()} == {
        "Android": 2,
        "iOS": 1,
        "Veb-sayt": 0,
        "Qurilmasiz": 1,
    }
    for row in platforms.values():
        assert "/accounts/authsession/" in row["href"]
        assert rows_behind(ops_client, row["href"]) == row["raw"], row["label"]
    sessions = _tile(data, "Ochiq sessiyalar")
    assert (sessions["value"], sessions["unit"]) == ("4", "sessiya")
    assert rows_behind(ops_client, sessions["href"]) == 4


# --- ops: the headline, the outbox thresholds ---------------------------------------------------------------


def test_the_headline_counts_problems_and_leaves_zero_parts_out() -> None:
    _report({}, at=NOW - timedelta(hours=1))
    AuditEntry.objects.create(
        action=report.RECONCILE_ACTION, entity_type="loyalty", entity_id="", actor_type="system", after={}
    )
    data = ops.build(NOW)
    assert data["overall"]["key"] in (dashboards.OK, dashboards.NEUTRAL)
    assert (data["headline"], data["problems"]) == ("Hammasi joyida", [])
    OutboxMessage.objects.create(topic="push.order_status", payload={}, dedupe_key="d", status="dead")
    data = ops.build(NOW)
    assert data["headline"] == "1 ta jiddiy muammo"
    assert [problem["label"] for problem in data["problems"]] == ["Outbox"]


@override_settings(DEV_FEATURES_ALLOWED=False)
def test_the_headline_counts_readiness_and_beat_rows_not_only_tiles() -> None:
    """Fake providers under the production settings warn only in the readiness table: the banner turned
    orange while the headline said «0 ta jiddiy, 0 ta diqqat»."""
    _report({}, at=NOW - timedelta(hours=1))
    AuditEntry.objects.create(
        action=report.RECONCILE_ACTION, entity_type="loyalty", entity_id="", actor_type="system", after={}
    )
    data = ops.build(NOW)
    assert all(tile["state"]["key"] not in (dashboards.WARN, dashboards.CRITICAL) for tile in data["tiles"])
    assert data["overall"]["key"] == dashboards.WARN
    assert data["headline"].endswith("ta ogohlantirish") and "jiddiy" not in data["headline"]
    assert "0 ta" not in data["headline"]
    assert {problem["label"] for problem in data["problems"]} >= {"Click", "Payme"}


def test_the_headline_counts_a_problem_once_across_tile_readiness_and_beat() -> None:
    data = ops.build(NOW)  # the report and the reconciliation never ran: tile + two beat rows
    labels = [problem["label"] for problem in data["problems"]]
    assert labels.count("Konsistentlik hisoboti") == 1
    assert "Cashback daftari solishtiruvi" in labels
    critical = [problem for problem in data["problems"] if problem["state"]["key"] == dashboards.CRITICAL]
    assert data["headline"].startswith(f"{len(critical)} ta jiddiy muammo")


def test_one_late_outbox_message_reads_the_same_in_the_table_the_tile_and_the_dispatcher_row() -> None:
    OutboxMessage.objects.create(
        topic="sms.send",
        payload={},
        dedupe_key="late-sms",
        status="pending",
        available_at=NOW - timedelta(minutes=95),
    )
    data = ops.build(NOW)
    row = next(row for row in data["outbox"] if row["topic"] == "sms.send")
    dispatcher = next(beat for beat in data["beat"] if beat["task"] == "Outbox dispetcheri")
    assert row["state"]["key"] == dispatcher["state"]["key"] == _tile(data, "Outbox")["state"]["key"]
    assert row["state"]["key"] == dashboards.WARN


def test_many_overdue_messages_are_critical_everywhere() -> None:
    for n in range(ops.OUTBOX_OVERDUE_CRITICAL):
        OutboxMessage.objects.create(
            topic="sms.send",
            payload={},
            dedupe_key=f"late-{n}",
            status="pending",
            available_at=NOW - timedelta(hours=3),
        )
    data = ops.build(NOW)
    dispatcher = next(beat for beat in data["beat"] if beat["task"] == "Outbox dispetcheri")
    assert data["outbox"][0]["state"]["key"] == dispatcher["state"]["key"] == dashboards.CRITICAL


def test_bts_and_billz_tiles_in_fake_mode_are_muted_not_an_alarm() -> None:
    from glowderma.integrations.models import IntegrationState

    IntegrationState.objects.filter(provider="bts").update(consecutive_failures=4)
    data = ops.build(NOW)
    bts = _tile(data, "BTS kuzatuvi")
    assert bts["muted"] is True and "xato" not in bts["value"] and "4 ketma-ket xato" in bts["hint"]
    assert _tile(data, "Billz sinxroni")["muted"] is True


def test_the_jobs_tile_of_an_empty_shop_has_a_clean_hint() -> None:
    tile = _tile(ops.build(NOW), "Fon vazifalari")
    assert tile["hint"] == "Billz import hali ishga tushirilmagan"


# --- the nightly report's severity --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("checks", "expected"),
    [
        ({"k6_outbox_overdue": {"count": 3, "ids": [1, 2, 3]}}, dashboards.WARN),
        ({"k6_outbox_overdue": {"count": 30, "ids": []}}, dashboards.WARN),  # many rows, still a warning
        ({"k2_payment_pending_stuck": {"count": 1, "ids": [7]}}, dashboards.CRITICAL),
        ({"k3_refund_open": {"count": 0, "ids": [], "error": "timeout"}}, dashboards.CRITICAL),
        ({"something_new": {"count": 2, "ids": []}}, dashboards.WARN),
        ({"k3_refund_open": {"count": 0, "ids": []}}, dashboards.OK),
    ],
)
def test_report_tiles_take_the_worst_severity_of_what_was_found(
    checks: dict[str, dict[str, Any]], expected: str
) -> None:
    _report(checks, at=NOW - timedelta(hours=1))
    assert _tile(ops.build(NOW), "Konsistentlik hisoboti")["state"]["key"] == expected
    assert _tile(money.build(NOW), "Oxirgi tungi hisobot")["state"]["key"] == expected


def test_a_ledger_reconciliation_that_never_ran_is_judged_alike_on_ops_and_money() -> None:
    beat = next(row for row in ops.build(NOW)["beat"] if row["task"] == "Cashback daftari solishtiruvi")
    tile = _tile(money.build(NOW), "Daftar solishtiruvi")
    assert beat["state"]["key"] == tile["state"]["key"] == dashboards.CRITICAL


@pytest.mark.parametrize(("hours", "expected"), [(40, dashboards.WARN), (80, dashboards.CRITICAL)])
def test_a_stale_report_is_judged_alike_on_ops_and_money(hours: int, expected: str) -> None:
    _report({}, at=NOW - timedelta(hours=hours))
    assert _tile(ops.build(NOW), "Konsistentlik hisoboti")["state"]["key"] == expected
    assert _tile(money.build(NOW), "Oxirgi tungi hisobot")["state"]["key"] == expected


def test_reconciliation_checks_are_named_in_uzbek() -> None:
    AuditEntry.objects.create(
        action=report.RECONCILE_ACTION,
        entity_type="loyalty",
        entity_id="",
        actor_type="system",
        after={"accounts": 3, "mismatches": 0, "checks": {"chain": 0, "ledger_sum": 1, "brand_new": 0}},
    )
    labels = {row["name"]: row["label"] for row in money.build(NOW)["reconcile_checks"]}
    assert labels["chain"].startswith("Yozuvlar zanjiri")
    assert labels["ledger_sum"] == "Balans = daftar yozuvlari yig‘indisi"
    assert labels["brand_new"] == "brand_new"  # an unknown check keeps its name


# --- the order lookup on the Ops banner ---------------------------------------------------------------------


def test_the_order_lookup_opens_the_order_or_explains(ops_client: Client) -> None:
    from glowderma.commerce.tests.factories import OrderFactory

    order = OrderFactory.create()
    page = ops_client.get("/django-admin/").content.decode()
    assert 'action="/django-admin/commerce/order/lookup/"' in page and "Buyurtma №" in page
    found = ops_client.get("/django-admin/commerce/order/lookup/", {"number": f"№{order.pk}"})
    assert (
        found.status_code == 302 and found["Location"] == f"/django-admin/commerce/order/{order.pk}/change/"
    )
    for number in ("999999", "abc", ""):
        missing = ops_client.get("/django-admin/commerce/order/lookup/", {"number": number})
        assert missing.status_code == 302 and missing["Location"] == "/django-admin/commerce/order/"


def test_the_order_lookup_needs_the_console(client: Client) -> None:
    response = client.get("/django-admin/commerce/order/lookup/", {"number": "1"})
    assert response.status_code == 302 and "/login/" in response["Location"]


# --- the demo data ------------------------------------------------------------------------------------------


def test_seed_ops_demo_writes_coherent_outbox_rows() -> None:
    """The seeded messages have the shape their handlers read (``billz.push_sale``: purchase, order, Billz
    uuid of a paid order), real topics, and were never touched before they were created."""
    from io import StringIO

    from django.core.management import call_command

    from glowderma.accounts.tests.factories import CustomerFactory
    from glowderma.catalog.tests.factories import ProductFactory
    from glowderma.loyalty.tests.factories import PurchaseFactory

    CustomerFactory.create_batch(2)
    ProductFactory.create()
    purchase = PurchaseFactory.create()
    call_command("seed_ops_demo", stdout=StringIO())
    rows = list(OutboxMessage.objects.filter(dedupe_key__startswith="ops-demo:outbox:"))
    assert rows and all(row.updated_at >= row.created_at for row in rows)
    pushes = [row for row in rows if row.topic == "billz.push_sale"]
    assert all(set(row.payload) == {"purchase_id", "order_id", "billz_order_uuid"} for row in pushes)
    first = next(row for row in pushes if row.payload["purchase_id"] is not None)
    assert first.payload == {
        "purchase_id": purchase.pk,
        "order_id": purchase.order_id,
        "billz_order_uuid": str(purchase.billz_order_uuid),
    }
    assert purchase.order is not None and purchase.order.paid_at is not None
    assert {row.topic for row in rows} <= {
        "billz.push_sale",
        "push.order_status",
        "sms.send",
        "bts.create_shipment",
        "campaign.send_batch",
    }


# --- money: guidance, the report button, capped lists ------------------------------------------------------


def test_k2_and_k3_rows_say_what_to_do_and_the_report_button_reuses_the_action(ops_client: Client) -> None:
    from glowderma.core.tests.console import console_client
    from glowderma.payments.tests.factories import RefundFactory

    RefundFactory.create(failed=True)
    data = money.build(NOW)
    rows = {row["name"]: row for row in data["rows"]}
    assert "Qaytarishlar" in rows["k3_refund_failed"]["guidance"]
    assert rows["k2_payment_pending_stuck"]["guidance"] == ""  # nothing found: no advice
    assert data["run_report_url"].startswith("/django-admin/core/auditentry/run-consistency-report/?next=")
    staff_page = ops_client.get("/django-admin/dashboards/money/").content.decode()
    assert "Hisobotni hozir ishga tushirish" not in staff_page  # managers only, like the action
    manager_page = console_client("manager").get("/django-admin/dashboards/money/").content.decode()
    assert "Hisobotni hozir ishga tushirish" in manager_page
    confirm = console_client("manager").get(data["run_report_url"])
    assert confirm.status_code == 200 and confirm.context["action_title"].startswith("Moliyaviy")


def test_a_capped_id_list_says_so() -> None:
    assert dashboards.capped(234, list(range(100))) == "Ro‘yxatda 100 / 234"
    assert dashboards.capped(5, [1, 2, 3, 4, 5]) is None
    assert dashboards.capped(0, []) is None


def test_seo_errors_are_danger_and_channels_keep_one_colour() -> None:
    from glowderma.reporting.dashboards import sales

    data = sales.build(NOW, 30)
    assert all(
        row["state"]["key"] == (dashboards.CRITICAL if row["state"]["label"] == "Xato" else dashboards.WARN)
        for row in data["seo"]
    )
    slots = {row["key"]: row["slot"] for row in data["channels"]}
    assert slots == {"web": 1, "ios": 2, "android": 3, "pos": 4}
    assert customers.CHANNEL_SLOTS == {key: slots[key] for key in ("web", "ios", "android")}
