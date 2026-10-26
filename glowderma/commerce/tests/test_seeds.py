"""commerce.seeds — one slow test: a fresh ``seed_demo`` reproduces the contract vectors out of production
code (design 03 §7.7/§7.8): the §4 №10248 history and §2.2 detail, Malika's card numbers, customer 13's
negative balance, the status counts and the dashboard numbers at the anchor; a second run adds nothing."""

from datetime import datetime
from io import StringIO
from typing import Any

import pytest
from django.conf import settings
from django.core.management import call_command
from django.test import override_settings
from rest_framework.test import APIClient

from glowderma.accounts.models import Customer
from glowderma.catalog.models import Product
from glowderma.commerce.models import Order, OrderEvent, StockReservation
from glowderma.core import seeding
from glowderma.core.models import AuditEntry, OutboxMessage
from glowderma.core.seeding import pinned_clock
from glowderma.delivery.models import Shipment, ShipmentEvent
from glowderma.loyalty import selectors as loyalty_selectors
from glowderma.loyalty.models import LedgerEntry, LoyaltyAccount, Purchase
from glowderma.loyalty.selectors import AccountSummary, Totals
from glowderma.payments.models import Payment, Refund

pytestmark = pytest.mark.django_db
ANCHOR = datetime.fromisoformat(seeding.DEFAULT_ANCHOR)

HISTORY_10248: list[dict[str, Any]] = [  # contract §4 test vector, newest first
    {
        "id": 7,
        "type": "handed_to_courier",
        "at": "2026-10-23T16:40:00+05:00",
        "source": "bts",
        "status_after": "handed_to_courier",
        "actor": None,
        "data": {"via": "webhook", "bts_status": "200", "push_sent": True},
    },  # noqa: E501
    {
        "id": 6,
        "type": "shipment_created",
        "at": "2026-10-23T14:06:00+05:00",
        "source": "system",
        "status_after": None,
        "actor": None,
        "data": {"tracking_number": "48213907", "tariff": 25000, "manual": False},
    },  # noqa: E501
    {
        "id": 5,
        "type": "billz_sale_recorded",
        "at": "2026-10-23T14:05:20+05:00",
        "source": "system",
        "status_after": None,
        "actor": None,
        "data": {"billz_order_id": "0d6f6c1e-5a7b-4c1e-9a43-2f1d8e3b7a10"},
    },  # noqa: E501
    {
        "id": 4,
        "type": "cashback_credited",
        "at": "2026-10-23T14:05:00+05:00",
        "source": "system",
        "status_after": None,
        "actor": None,
        "data": {
            "ledger_entry_id": 121,
            "amount": 5680,
            "base": 568000,
            "percent": "1.00",
            "policy_version": 3,
        },
    },  # noqa: E501
    {
        "id": 3,
        "type": "payment_confirmed",
        "at": "2026-10-23T14:05:00+05:00",
        "source": "system",
        "status_after": "processing",
        "actor": None,
        "data": {
            "provider": "click",
            "transaction_id": "CLK-7741920385",
            "amount": 784400,
            "reservation_converted": True,
        },
    },  # noqa: E501
    {
        "id": 2,
        "type": "reservation_created",
        "at": "2026-10-23T14:02:00+05:00",
        "source": "system",
        "status_after": None,
        "actor": None,
        "data": {"item_count": 3, "minutes": 15, "expires_at": "2026-10-23T14:17:00+05:00"},
    },  # noqa: E501
    {
        "id": 1,
        "type": "order_created",
        "at": "2026-10-23T14:02:00+05:00",
        "source": "system",
        "status_after": "awaiting_payment",
        "actor": None,
        "data": {"channel": "ios", "item_count": 3, "total": 784400},
    },  # noqa: E501
]


def _item(
    product_id: int, name: str, billz_id: str, quantity: int, price: int, promo: bool
) -> dict[str, Any]:
    return {
        "product_id": product_id,
        "name": name,
        "billz_id": billz_id,
        "image_url": "<media>",
        "quantity": quantity,
        "unit_price": price,
        "promo": promo,
    }


DETAIL_10248: dict[str, Any] = {  # contract §2.2 example (ids pinned, image URLs normalised)
    "id": 10248,
    "customer": {"id": 5, "name": "Malika Karimova", "phone": "+998901234567"},
    "created_at": "2026-10-23T14:02:00+05:00",
    "total": 784400,
    "status": "handed_to_courier",
    "payment": {
        "status": "paid",
        "provider": "click",
        "amount": 784400,
        "transaction_id": "CLK-7741920385",
        "confirmed_at": "2026-10-23T14:05:00+05:00",
    },
    "fulfilment": "delivery",
    "shipment": {
        "state": "created",
        "tracking_number": "48213907",
        "tariff": 25000,
        "updated_at": "2026-10-23T16:40:00+05:00",
        "via": "webhook",
    },
    "cancellation": None,
    "channel": "ios",
    "address": "Toshkent, Chilonzor tumani, 9-kvartal, 12-uy, 45-xonadon",
    "items": [
        _item(4, "Seramidli namlantiruvchi krem", "4607012345", 1, 312000, False),
        _item(2, "Quyoshdan himoya kremi SPF 50", "4607012401", 1, 240000, True),
        _item(3, "Yumshoq tozalovchi ko‘pik", "4607012388", 2, 128000, False),
    ],
    "items_subtotal": 808000,
    "promo_subtotal": 240000,
    "delivery_fee": 25000,
    "cashback_used": 48600,
    "refunds": [],
    "cashback_hold": None,
    "cashback": [
        {
            "ledger_entry_id": 120,
            "kind": "debit",
            "amount": -48600,
            "at": "2026-10-23T14:05:00+05:00",
            "base": None,
            "percent": None,
            "reverses_id": None,
        },  # noqa: E501
        {
            "ledger_entry_id": 121,
            "kind": "credit",
            "amount": 5680,
            "at": "2026-10-23T14:05:00+05:00",
            "base": 568000,
            "percent": "1.00",
            "reverses_id": None,
        },  # noqa: E501
    ],
    "history": HISTORY_10248,
    "available_actions": {"transitions": ["delivered"], "cancel": True, "retry_shipment": False},
}
COUNTED = (
    Order,
    OrderEvent,
    StockReservation,
    Payment,
    Refund,
    Shipment,
    ShipmentEvent,
    LedgerEntry,
    Purchase,
    LoyaltyAccount,
    OutboxMessage,
    AuditEntry,
)


def _counts() -> dict[str, int]:
    return {model.__name__: model.objects.count() for model in COUNTED}


def test_seed_demo_reproduces_the_design_vectors(staff_client: APIClient) -> None:
    call_command("seed_demo", force=True, verbosity=0)
    first = _counts()

    # --- order №10248: contract §4 history and §2.2 detail out of the replayed flows ---
    detail = staff_client.get("/api/v1/admin/orders/10248/").json()
    base = str(settings.PUBLIC_MEDIA_BASE_URL)
    for item in detail["items"]:
        assert item["image_url"].startswith(f"{base}products/{item['product_id']}/")
        item["image_url"] = "<media>"
    assert detail == DETAIL_10248

    # --- Malika (5): the drawn chain 100–112 + 120/121, totals and balance (03 §7.7) ---
    ledger = loyalty_selectors.ledger_for_customer(5)
    assert [(row.id, row.kind, row.amount, row.balance_after) for row in reversed(ledger)] == [
        (100, "credit", 3_610, 3_610),
        (101, "credit", 12_450, 16_060),
        (102, "credit", 9_870, 25_930),
        (103, "credit", 15_300, 41_230),
        (104, "credit", 6_210, 47_440),
        (105, "credit", 8_640, 56_080),
        (106, "credit", 1_560, 57_640),
        (107, "reversal", -1_560, 56_080),
        (108, "credit", 4_120, 60_200),
        (109, "adjustment", 2_950, 63_150),
        (110, "debit", -20_000, 43_150),
        (111, "credit", 3_140, 46_290),
        (112, "credit", 2_310, 48_600),
        (120, "debit", -48_600, 0),
        (121, "credit", 5_680, 5_680),
    ]
    assert loyalty_selectors.customer_totals(5) == Totals(9_940_000, 3_640_000, 13_580_000, 12)
    assert loyalty_selectors.account_summary(5) == AccountSummary(5_680, 0, 5_680)
    assert len(loyalty_selectors.purchases_for_customer(5)) == 25
    adjustment = AuditEntry.objects.get(action="cashback.adjusted", entity_id="5")
    assert adjustment.after["balance_after"] == 63_150 and adjustment.actor_id is not None  # type: ignore[index]

    # --- customer 13: the deliberate negative balance (02 §4.5 item 4) ---
    assert list(LoyaltyAccount.objects.filter(balance__lt=0).values_list("customer_id", flat=True)) == [13]
    assert Order.objects.get(pk=10210).status == "cancelled" and Order.objects.get(pk=10215).cashback_used > 0
    assert Order.objects.filter(customer_id=13).count() == 2

    # --- fixed rows (03 §7.8) ---
    order_10250 = staff_client.get("/api/v1/admin/orders/10250/").json()
    assert order_10250["shipment"]["state"] == "failed" and order_10250["shipment"]["attempts"] == 1
    assert order_10250["shipment"]["pending"] is False and order_10250["available_actions"]["retry_shipment"]
    order_10244 = staff_client.get("/api/v1/admin/orders/10244/").json()
    assert order_10244["shipment"] == {"state": "not_created", "pending": False}
    assert (
        order_10244["payment"]["status"] == "refunded" and order_10244["refunds"][0]["state"] == "succeeded"
    )
    assert order_10244["cancellation"] == {"reason": "admin", "note": "Mijoz so‘rovi bilan"}
    order_10249 = staff_client.get("/api/v1/admin/orders/10249/").json()
    assert order_10249["history"][0]["data"] == {"released_item_count": 1, "payment_failed": True}
    assert order_10249["payment"]["status"] == "failed"
    order_10198 = Order.objects.get(pk=10198)
    assert order_10198.status == "cancelled" and Shipment.objects.get(order=order_10198).state == "failed"
    expires = Order.objects.get(pk=10252).reservation_expires_at
    assert expires is not None and expires > ANCHOR  # keep_open
    assert Order.objects.get(pk=10246).delivered_at is not None
    tracking = dict(
        Shipment.objects.filter(order_id__in=[10243, 10245, 10246]).values_list("order_id", "tracking_number")
    )
    assert tracking == {10243: "48207320", 10245: "48209981", 10246: "48211544"}

    # --- status counts, nav counts, dashboard at the anchor (03 §7.8 "resulting totals") ---
    with pinned_clock(ANCHOR):
        counts = staff_client.get("/api/v1/admin/orders/status-counts/").json()
        assert counts == {
            "all": 252,
            "awaiting_payment": 6,
            "processing": 12,
            "handed_to_courier": 9,
            "delivered": 213,
            "cancelled": 12,
        }
        assert staff_client.get("/api/v1/admin/nav-counts/").json() == {
            "orders_processing": 12,
            "appointments_pending": 3,
        }
        dashboard = staff_client.get("/api/v1/admin/dashboard/").json()
        recent = staff_client.get("/api/v1/admin/orders/", {"page_size": 6}).json()
    assert [row["id"] for row in recent["results"]] == [10252, 10251, 10250, 10249, 10248, 10247]
    assert len(dashboard["buckets"]) == 24 and dashboard["bucket"] == "hour"
    assert (dashboard["stats"]["orders"], dashboard["stats"]["revenue"]) == (3, 768_000)
    assert dashboard["previous_period_orders"] == 4
    assert (dashboard["stats"]["total_customers"], dashboard["stats"]["deleted_customers"]) == (432, 6)
    assert dashboard["stats"]["out_of_stock"] == 7
    assert dashboard["attention"] == {
        "bts_failed": 1,
        "pending_appointments": 3,
        "next_appointment_at": "2026-10-26T09:00:00+05:00",
        "new_from_billz": 4,
        "refunds_needing_action": 0,
        "bts_polling_failing": False,
    }
    assert sum(bucket["revenue"] for bucket in dashboard["buckets"]) == 768_000
    assert (
        not OutboxMessage.objects.filter(
            topic__in=["bts.create_shipment", "billz.push_sale", "refund.submit"]
        )
        .exclude(status="done")
        .exists()
    )  # noqa: E501

    # --- idempotent: a second run adds nothing ---
    call_command("seed_demo", force=True, verbosity=0)
    assert _counts() == first


def test_seed_demo_refuses_a_database_seeded_before_the_orders_slice() -> None:
    """Loyalty demo rows without the commerce demo orders: the replay would reuse their ledger ids, so
    ``seed_demo`` stops up front and points at ``--reset``; nothing is written."""
    from django.core.management.base import CommandError

    from glowderma.accounts.tests.factories import CustomerFactory, StaffUserFactory
    from glowderma.commerce import seeds as commerce_seeds
    from glowderma.loyalty import services as loyalty_services

    customer = CustomerFactory.create()
    with pinned_clock(ANCHOR):
        loyalty_services.adjust(
            customer.pk, "add", 1_000, "Oldingi demo yozuvi", StaffUserFactory.create().pk
        )
    assert commerce_seeds.preflight() == commerce_seeds.MESSAGE_SEEDED_BEFORE_ORDERS
    with pytest.raises(CommandError, match=r"seed_demo --reset") as refused:
        call_command("seed_demo", force=True)
    assert "seed_store_sales or manual QA" in str(refused.value)  # the refusal says how broad it is
    assert not Order.objects.exists() and LedgerEntry.objects.count() == 1


def test_seed_demo_force_under_the_production_settings_skips_the_demo_orders(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Staging runs the production settings with fakes and seeds with ``--force``: the run succeeds, the demo
    orders are skipped (their effects would go through ports that are live or report no effect there) and
    the store sales are still recorded."""
    out = StringIO()
    with override_settings(DEV_FEATURES_ALLOWED=False), caplog.at_level("WARNING"):
        call_command("seed_demo", force=True, verbosity=0, stdout=out)
    assert not Order.objects.exists()
    assert "seed.commerce_orders_skipped" in caplog.text
    assert Customer.objects.exists() and Product.objects.exists()
    # the operator sees the skip next to "ran: … commerce", not only in the log
    assert "commerce: demo orders skipped under the production settings" in out.getvalue()
    from glowderma.core.management.commands.seed_demo import Command

    usage = " ".join(Command().create_parser("manage.py", "seed_demo").format_help().split())
    assert "the commerce seeder skips the demo orders" in usage


def test_seed_demo_reports_no_skip_under_the_local_settings() -> None:
    from glowderma.commerce import seeds
    from glowderma.core.seeding import SeedContext

    context = SeedContext(anchor=ANCHOR, keep_open=True)
    seeds.seed_commerce(context)  # standalone (no lower layers): store sales only, nothing skipped
    assert context.notes == []


def test_the_seeder_delivery_fee_never_calls_the_bts_port() -> None:
    from glowderma.commerce import seeds

    plan = next(p for p in seeds.fixed_orders() if p.id == 10252)  # Samarqand (region 13)
    with override_settings(DEV_FEATURES_ALLOWED=False):  # the port would refuse every quote here
        assert seeds._delivery_fee(plan) == 28_000
