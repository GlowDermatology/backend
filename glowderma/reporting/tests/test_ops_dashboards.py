"""The four ops-console dashboards (glowderma.reporting.dashboards, admin_views): access, numbers, thresholds,
query bounds, the minute cache with its refresh link, and every link target."""

import re
from collections.abc import Iterator
from datetime import datetime, time, timedelta
from html import unescape
from io import StringIO
from typing import Any

import pytest
from django.contrib.auth.models import AnonymousUser
from django.core.cache import cache
from django.core.cache import cache as django_cache
from django.core.management import CommandError, call_command
from django.db import connection
from django.test import Client, RequestFactory, override_settings
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from glowderma.accounts.tests.factories import (
    CustomerFactory,
    CustomerSessionFactory,
    CustomerSocialIdentityFactory,
    DeviceFactory,
    SmsCodeFactory,
    StaffUserFactory,
)
from glowderma.appointments.tests.factories import AppointmentFactory
from glowderma.catalog.tests.factories import ProductFactory, ProductStockFactory
from glowderma.commerce.tests.factories import OrderFactory
from glowderma.core import audit, enums, seeding
from glowderma.core.models import AuditEntry, OutboxMessage
from glowderma.core.timeutils import TASHKENT
from glowderma.integrations.models import IntegrationState
from glowderma.integrations.tests.factories import JobFactory, WebhookInboxFactory
from glowderma.loyalty.tests.factories import (
    CashbackPolicyFactory,
    LedgerEntryFactory,
    LoyaltyAccountFactory,
    PurchaseFactory,
)
from glowderma.payments.tests.factories import PaymentFactory, RefundFactory
from glowderma.professionals.tests.factories import ProfessionalFactory, WorkingHoursFactory
from glowderma.reporting import admin_views, dashboards
from glowderma.reporting.dashboards import customers, money, ops, report, sales
from glowderma.reporting.tasks import run_consistency_report

pytestmark = pytest.mark.django_db
NOW = datetime(2026, 10, 31, 12, 0, tzinfo=TASHKENT)
INDEX = "/django-admin/"
PAGES = {
    "ops": INDEX,
    "money": "/django-admin/dashboards/money/",
    "sales": "/django-admin/dashboards/sales/",
    "customers": "/django-admin/dashboards/customers/",
}
HREF = re.compile(r'href="(/django-admin/[^"#]*)"')


@pytest.fixture(autouse=True)
def _pinned() -> Iterator[None]:
    cache.clear()
    with seeding.pinned_clock(NOW):
        yield


def _dead(topic: str, order_id: int, *, hours_ago: int = 2) -> OutboxMessage:
    message = OutboxMessage.objects.create(
        topic=topic,
        payload={"order_id": order_id},
        dedupe_key=f"dead:{topic}:{order_id}:{OutboxMessage.objects.count()}",
        status="dead",
        available_at=NOW - timedelta(hours=hours_ago),
    )
    OutboxMessage.objects.filter(pk=message.pk).update(updated_at=NOW - timedelta(hours=hours_ago))
    return message


def _report_at(instant: datetime) -> None:
    """The nightly report as if it ran at ``instant`` (its audit row is stamped by the clock)."""
    with seeding.pinned_clock(instant):
        run_consistency_report(instant)


def _buyer() -> Any:
    """The one customer behind the factory rows (registered long ago: not a sign-up of the window)."""
    return CustomerFactory.create(registered_at=NOW - timedelta(days=400))


def _paid_order(
    channel: str, amount: int, *, days_ago: int, product: Any = None, customer: Any = None
) -> Any:
    created = NOW - timedelta(days=days_ago, hours=1)
    product = product or ProductFactory.create(price=amount)
    order = OrderFactory.create(
        customer=customer or _buyer(),
        paid=True,
        channel=channel,
        created_at=created,
        items_subtotal=amount,
        items=[{"product": product, "quantity": 1, "unit_price": amount}],
    )
    PaymentFactory.create(order=order, amount=order.total, confirmed_at=order.paid_at)
    return order


def populate() -> dict[str, Any]:
    """A shop with something to show on every dashboard (each value the tests assert)."""
    buyer = _buyer()
    OutboxMessage.objects.create(
        topic="sms.send",
        payload={},
        dedupe_key="late",
        status="pending",
        available_at=NOW - timedelta(minutes=90),
    )
    WebhookInboxFactory.create(
        received_at=NOW - timedelta(hours=1),
        result={"status": 200, "body": {}, "outcome": "rejected", "error_code": "-1"},
    )
    WebhookInboxFactory.create(
        received_at=NOW - timedelta(hours=2),
        result={"status": 200, "body": {}, "outcome": "accepted", "error_code": None},
    )
    IntegrationState.objects.filter(provider="bts").update(consecutive_failures=4)
    JobFactory.create(
        state=enums.JobState.FAILED,
        created_at=NOW - timedelta(hours=3),
        started_at=NOW - timedelta(hours=3),
        finished_at=NOW - timedelta(hours=3) + timedelta(seconds=42),
        error={"code": "billz", "message": "503"},
    )
    serum = ProductFactory.create(price=1_250_000, name_uz="Retinolli tungi serum")
    first = _paid_order("web", 1_250_000, days_ago=1, product=serum, customer=buyer)
    _paid_order("web", 1_250_000, days_ago=2, product=serum, customer=buyer)
    android = _paid_order("android", 300_000, days_ago=3, customer=buyer)
    refunded = _paid_order("ios", 500_000, days_ago=4, customer=buyer)
    _dead("billz.push_sale", first.pk)
    _dead("push.order_status", android.pk)
    RefundFactory.create(payment=refunded.payments.get(), amount=refunded.total, failed=True)
    PurchaseFactory.create(store=True, customer=buyer, amount=231_000, at=NOW - timedelta(days=1))
    ProductStockFactory.create(quantity=0, synced_at=NOW - timedelta(hours=1))
    policy = CashbackPolicyFactory.create(created_at=NOW - timedelta(days=40), tiers=[(0, None, "1.00")])
    account = LoyaltyAccountFactory.create(customer=buyer)
    LedgerEntryFactory.create(
        account=account, order=first, amount=12_500, at=NOW - timedelta(days=2), policy=policy
    )
    LedgerEntryFactory.create(
        account=account, order=android, debit=True, amount=-4_000, at=NOW - timedelta(days=2)
    )
    registered = NOW - timedelta(days=3)
    CustomerFactory.create(registered_at=registered)
    phoneless = CustomerFactory.create(
        phone=None, email="new@example.uz", email_verified_at=registered, registered_at=registered
    )
    CustomerSocialIdentityFactory.create(customer=phoneless, provider="google", created_at=registered)
    device = DeviceFactory.create(customer=buyer, platform="android")
    CustomerSessionFactory.create(customer=device.customer, device=device)
    SmsCodeFactory.create(
        created_at=NOW - timedelta(hours=2), attempts=5, exhausted_at=NOW - timedelta(hours=2)
    )
    audit.record(action="auth.login_failed", entity_type="staff", entity_id="x")
    specialist = ProfessionalFactory.create(name="Nodira Rahimova")
    WorkingHoursFactory.create(
        professional=specialist, weekday=0, start=time(9), end=time(18)
    )  # Mondays only
    conflict = AppointmentFactory.create(
        professional=specialist, customer=buyer, start=NOW + timedelta(days=1, hours=-2)
    )
    AppointmentFactory.create(customer=buyer, start=NOW.replace(hour=10))  # today, already started
    _report_at(NOW - timedelta(hours=5))
    return {"conflict": conflict, "serum": serum}


def _get(client: Client, url: str) -> Any:
    response = client.get(url)
    assert response.status_code == 200, (url, response.status_code)
    return response


# --- access -------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("url", list(PAGES.values()))
def test_anonymous_visitors_are_sent_to_the_login(client: Client, url: str) -> None:
    response = client.get(url)
    assert response.status_code == 302
    assert response["Location"].startswith("/django-admin/login/")


@pytest.mark.parametrize("url", list(PAGES.values()))
def test_signed_in_non_staff_never_see_a_dashboard(client: Client, url: str) -> None:
    client.force_login(StaffUserFactory.create(is_staff=False))
    response = client.get(url)
    assert response.status_code in (302, 403)
    assert "dashboard" not in (response.context or {})


def test_the_view_refuses_a_non_staff_user_even_past_the_site_wrapper() -> None:
    request = RequestFactory().get(PAGES["money"])
    request.user = StaffUserFactory.create(is_staff=False)
    view = admin_views.MoneyDashboardView(admin_site=None)
    view.setup(request)
    assert not view.has_permission()
    request.user = AnonymousUser()
    assert admin_views.dashboard_callback(request, {"title": "x"}) == {"title": "x"}


def test_the_sidebar_lists_the_four_dashboards_first(console: Client) -> None:
    response = _get(console, INDEX)
    first = response.context["sidebar_navigation"][0]
    assert first["title"] == "Boshqaruv"
    assert [item["title"] for item in first["items"]] == [
        "Ops holati",
        "Pul solishtirish",
        "Savdo va katalog",
        "Mijozlar va kirish",
    ]
    assert [str(item["link"]) for item in first["items"]] == list(PAGES.values())


# --- rendering: the numbers ---------------------------------------------------------------------------------


def test_ops_dashboard_renders_the_traffic_lights(console: Client) -> None:
    populate()
    response = _get(console, INDEX)
    data = response.context["dashboard"]
    tiles = {tile["title"]: tile for tile in data["tiles"]}
    assert data["outbox_totals"] == {"dead": 2, "overdue": 1, "open": 1}
    assert tiles["Outbox"]["state"]["key"] == dashboards.CRITICAL
    assert data["webhook_totals"] == {"failed": 1, "total": 2}
    assert tiles["Webhooklar · 24 soat"]["state"]["key"] == dashboards.WARN
    assert tiles["BTS kuzatuvi"]["state"]["label"] == "Sinov rejimi"  # fake mode: informational
    assert tiles["Fon vazifalari"]["state"]["key"] == dashboards.CRITICAL  # the last import failed
    assert tiles["Konsistentlik hisoboti"]["value"] == str(data["report"]["findings"])
    assert data["overall"]["key"] == dashboards.CRITICAL
    page = response.content.decode()
    for text in ("Ops holati", "billz.push_sale", "Yangilangan: <strong", "12:00", "Provayderlar tayyorligi"):
        assert text in page
    assert response.templates[0].name == admin_views.OPS_TEMPLATE


def test_money_dashboard_shows_the_report_next_to_the_live_checks(console: Client) -> None:
    populate()
    data = _get(console, PAGES["money"]).context["dashboard"]
    rows = {row["name"]: row for row in data["rows"]}
    assert rows["k3_refund_failed"]["report"]["count"] == 1
    assert rows["k3_refund_failed"]["live"]["count"] == 1
    assert rows["k3_refund_failed"]["state"]["key"] == dashboards.CRITICAL
    assert rows["k6_outbox_dead"]["live"]["count"] == 2
    assert rows["k1_billz_sale_unrecorded"]["live"]["note"] == "Billz sinov rejimida — tekshirilmadi"
    assert rows["k2_payment_pending_stuck"]["state"]["key"] == dashboards.OK
    assert data["report"]["stale"] is False
    tiles = {tile["title"]: tile for tile in data["tiles"]}
    assert tiles["Cashback siyosati"]["state"]["key"] == dashboards.OK
    assert tiles["Daftar solishtiruvi"]["value"] == "0"


def test_money_dashboard_warns_when_the_report_never_ran_or_is_stale(console: Client) -> None:
    page = _get(console, PAGES["money"]).content.decode()
    assert "Tungi hisobot hali hech qachon ishlamagan" in page
    _report_at(NOW - timedelta(hours=40))
    cache.clear()
    data = _get(console, PAGES["money"]).context["dashboard"]
    assert data["report"]["stale"] is True
    tile = data["tiles"][0]
    assert tile["state"]["key"] == dashboards.WARN and tile["hint"].startswith("Eskirgan")


def test_money_dashboard_flags_a_missing_policy_as_critical() -> None:
    data = money.build(NOW)
    policy = next(tile for tile in data["tiles"] if tile["title"] == "Cashback siyosati")
    assert (policy["value"], policy["state"]["key"]) == ("Yo‘q", dashboards.CRITICAL)


def test_sales_dashboard_splits_revenue_by_channel(console: Client) -> None:
    objects = populate()
    response = _get(console, PAGES["sales"] + "?days=7")
    data = response.context["dashboard"]
    channels = {row["key"]: row["raw"] for row in data["channels"]}
    assert channels["web"] == {"orders": 2, "revenue": 2_550_000}  # items + delivery fee, per payment
    assert channels["android"] == {"orders": 1, "revenue": 325_000}
    assert channels["ios"] == {"orders": 1, "revenue": 525_000}
    assert channels["pos"] == {"orders": 1, "revenue": 231_000}
    assert data["raw"]["online_revenue"] == 3_400_000 and data["raw"]["online_orders"] == 4
    assert data["raw"]["aov"] == 850_000
    assert data["top"][0]["name"] == "Retinolli tungi serum"
    assert data["top"][0]["href"] == reverse("admin:catalog_product_change", args=[objects["serum"].pk])
    assert [window["active"] for window in data["windows"]] == [True, False, False]
    page = response.content.decode()
    assert "3,4 mln so‘m" in page and 'data-gd-chart="som"' in page and dashboards.som(2_550_000) in page


def test_sales_window_falls_back_to_30_days() -> None:
    assert [sales.window_days(raw) for raw in ("7", "90", "15", "x", None)] == [7, 90, 30, 30, 30]


def test_customers_dashboard(console: Client) -> None:
    objects = populate()
    data = _get(console, PAGES["customers"]).context["dashboard"]
    raw = data["raw"]
    assert raw["phoneless"] == 1 and raw["staff_failures"] == 1 and raw["conflicts"] == 1
    assert raw["appointments_today"] == 1
    assert raw["sessions"] == {"android": 1}
    methods = {row["label"]: row["raw"] for row in data["signups"]["methods"]}
    assert methods == {"Telefon": 1, "Email": 0, "Ijtimoiy tarmoq": 1}
    assert data["cashback"]["raw"] == {"issued": 12_500, "redeemed": 4_000}
    assert data["clinic"]["conflicts"][0]["id"] == objects["conflict"].pk
    otp = next(tile for tile in data["tiles"] if tile["title"] == "OTP · 24 soat")
    assert otp["state"]["key"] == dashboards.WARN  # one lockout


def test_empty_shop_renders_empty_states(console: Client) -> None:
    pages = {name: _get(console, url).content.decode() for name, url in PAGES.items()}
    assert "Navbat bo‘sh" in pages["ops"]
    assert "Bu davrda sotuv bo‘lmadi" in pages["sales"]
    assert "30 kun ichida yangi mijoz yo‘q" in pages["customers"]
    assert "Keyingi 14 kunda jadval ziddiyati yo‘q" in pages["customers"]


# --- thresholds ---------------------------------------------------------------------------------------------


def test_ops_report_tile_states() -> None:
    assert ops.build(NOW)["tiles"][4]["state"]["key"] == dashboards.CRITICAL  # never ran
    _report_at(NOW - timedelta(hours=2))
    assert ops.build(NOW)["tiles"][4]["state"]["key"] == dashboards.OK
    _dead("push.order_status", 9, hours_ago=0)
    _report_at(NOW)
    assert ops.build(NOW)["tiles"][4]["state"]["key"] == dashboards.WARN  # a finding


@override_settings(CLICK_MODE="live")
def test_a_live_provider_without_credentials_is_critical() -> None:
    rows = {row["provider"]: row for row in ops.build(NOW)["readiness"]}
    assert rows["Click"]["mode"] == "Live" and rows["Click"]["state"]["key"] == dashboards.CRITICAL
    assert rows["Payme"]["state"]["label"] == "Sinov rejimi"


def test_state_helpers() -> None:
    assert dashboards.by_count(0) == dashboards.OK
    assert dashboards.by_count(1, critical=3) == dashboards.WARN
    assert dashboards.by_count(3, critical=3) == dashboards.CRITICAL
    hour = timedelta(hours=1)
    assert dashboards.by_age(None, warn=hour, critical=2 * hour) == dashboards.CRITICAL
    assert dashboards.by_age(hour / 2, warn=hour, critical=2 * hour) == dashboards.OK
    assert dashboards.by_age(hour * 1.5, warn=hour, critical=2 * hour) == dashboards.WARN
    assert dashboards.worst([dashboards.OK, dashboards.NEUTRAL, dashboards.WARN]) == dashboards.WARN
    assert dashboards.som_short(12_400_000) == "12,4 mln so‘m" and dashboards.som_short(850) == "850 so‘m"
    assert dashboards.ago(NOW - timedelta(minutes=12), NOW) == "12 daqiqa oldin"
    assert dashboards.duration(185) == "3 daq 05 s"


def test_report_summary_reads_the_audit_shape() -> None:
    assert report.summary(None) is None
    entry = AuditEntry.objects.create(
        action=report.REPORT_ACTION,
        entity_type="report",
        entity_id="consistency",
        actor_type="system",
        after={
            "checks": {
                "a": {"count": 2, "ids": [5, 6]},
                "b": {"count": 0, "ids": [], "error": "failed"},
                "c": "junk",
            }
        },
    )
    summary = report.summary(entry)
    assert summary is not None
    assert (summary.findings, summary.failing, summary.errors) == (2, ["a", "b", "c"], ["b", "c"])
    assert summary.checks["a"].ids == [5, 6]


# --- queries and cache --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("build", "bound"),
    [
        (lambda: ops.build(NOW), 6),
        (lambda: money.build(NOW), 20),
        (lambda: sales.build(NOW, 30), 13),
        (lambda: customers.build(NOW), 15),
    ],
)
def test_each_dashboard_is_a_handful_of_statements(
    build: Any, bound: int, django_assert_max_num_queries: Any
) -> None:
    populate()
    with django_assert_max_num_queries(bound):
        build()


def test_query_count_does_not_grow_with_the_data(console: Client) -> None:
    populate()
    counts = []
    for _round in range(2):
        cache.clear()
        with CaptureQueriesContext(connection) as queries:
            for url in PAGES.values():
                _get(console, url)
        counts.append(len(queries))
        populate_more()
    assert counts[0] == counts[1]


def populate_more() -> None:
    for n in range(3):
        order = _paid_order("web", 200_000, days_ago=n)
        _dead(f"topic.{n}", order.pk)
        AppointmentFactory.create(customer=order.customer, start=NOW + timedelta(days=2, hours=n))


def test_a_dashboard_is_cached_for_a_minute_and_refresh_rebuilds_it(console: Client) -> None:
    _dead("billz.push_sale", 1)
    assert _get(console, INDEX).context["dashboard"]["outbox_totals"]["dead"] == 1
    _dead("billz.push_sale", 2)
    with CaptureQueriesContext(connection) as cached_queries:
        cached = _get(console, INDEX)
    assert cached.context["dashboard"]["outbox_totals"]["dead"] == 1
    assert not any("core_outboxmessage" in query["sql"] for query in cached_queries)
    fresh = _get(console, INDEX + "?refresh=1")
    assert fresh.context["dashboard"]["outbox_totals"]["dead"] == 2
    assert fresh.context["dashboard_links"]["refresh"] == "/django-admin/?refresh=1"
    assert _get(console, INDEX).context["dashboard"]["outbox_totals"]["dead"] == 2  # stored again


def test_sales_windows_are_cached_apart_and_refresh_keeps_the_window(console: Client) -> None:
    response = _get(console, PAGES["sales"] + "?days=90")
    assert response.context["dashboard"]["days"] == 90
    assert response.context["dashboard_links"]["refresh"] == PAGES["sales"] + "?days=90&refresh=1"
    assert _get(console, PAGES["sales"] + "?days=7").context["dashboard"]["days"] == 7


def test_a_cache_outage_still_renders(monkeypatch: pytest.MonkeyPatch, console: Client) -> None:
    def broken(*args: Any, **kwargs: Any) -> Any:
        raise ConnectionError("redis down")

    monkeypatch.setattr(django_cache, "get", broken)
    monkeypatch.setattr(django_cache, "set", broken)
    assert _get(console, PAGES["money"]).context["dashboard"]["rows"]


# --- links --------------------------------------------------------------------------------------------------


def test_every_link_on_every_dashboard_opens_for_staff(console: Client) -> None:
    populate()
    hrefs: set[str] = set()
    for url in PAGES.values():
        page = _get(console, url).content.decode()
        hrefs.update(unescape(href) for href in HREF.findall(page))
    dashboard_links = {href for href in hrefs if "/change/" in href or "?" in href}
    assert len(dashboard_links) > 40  # tiles, rows, samples
    for href in sorted(hrefs):
        if href.startswith("/django-admin/logout"):
            continue
        response = console.get(href)
        assert response.status_code == 200, (href, response.status_code, response.get("Location"))


# --- demo data command --------------------------------------------------------------------------------------


def test_seed_ops_demo_adds_signals_once() -> None:
    CustomerFactory.create_batch(2)
    ProductFactory.create()
    call_command("seed_ops_demo", stdout=StringIO())
    dead = OutboxMessage.objects.filter(status="dead").count()
    assert dead == 3 and AuditEntry.objects.filter(action=report.REPORT_ACTION).count() == 1
    call_command("seed_ops_demo", stdout=StringIO())
    assert OutboxMessage.objects.filter(status="dead").count() == dead


@override_settings(DEV_FEATURES_ALLOWED=False)
def test_seed_ops_demo_is_refused_in_production() -> None:
    with pytest.raises(CommandError):
        call_command("seed_ops_demo")
