"""Contract §2.6 ``GET customers/{id}/``: the full card built through the loyalty services (store sale, paid
online orders, an adjustment, a reversal, an active hold), the source_ref rule, groups, upcoming
appointments, the active device and every device (gap ADM-8.1), the newest order of any status and the
count of every order, deleted customers and 404."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext
from rest_framework.test import APIClient

from glowderma.accounts import services as accounts_services
from glowderma.accounts.models import Customer, StaffUser
from glowderma.accounts.tests.factories import DeviceFactory
from glowderma.appointments.tests.factories import AppointmentFactory
from glowderma.commerce.tests.factories import OrderFactory
from glowderma.core import enums
from glowderma.core.timeutils import TASHKENT
from glowderma.customers.tests.conftest import ANCHOR
from glowderma.customers.tests.helpers import make_customer
from glowderma.loyalty import services as loyalty_services
from glowderma.loyalty.models import CashbackPolicy, LedgerEntry
from glowderma.notifications.tests.factories import CustomerSegmentFactory, SegmentMemberFactory

pytestmark = pytest.mark.django_db

MLN = 1_000_000


def url(pk: int) -> str:
    return f"/api/v1/admin/customers/{pk}/"


def at(day: int, hour: int = 12, minute: int = 0) -> datetime:
    return datetime(2026, 10, day, hour, minute, tzinfo=TASHKENT)


def iso(value: datetime) -> str:
    return value.astimezone(TASHKENT).isoformat()


@pytest.fixture
def history(staff: StaffUser, policies: dict[int, CashbackPolicy]) -> dict[str, Any]:
    """A customer's October under v3 (1 % below 20 mln, earns on the cashback-paid part), in posting order."""
    customer = make_customer("Malika Karimova", phone="+998901234567", address_text="Toshkent, Chilonzor")
    store = loyalty_services.record_store_purchase(
        customer.pk, "S-1001", "S-1001", 1_000_000, sold_at=at(5, 10), at=at(5, 10)
    )
    order_a = OrderFactory.create(
        customer=customer,
        delivered=True,
        created_at=at(10, 11, 57),
        items_subtotal=500_000,
        promo_subtotal=100_000,
    )
    online_a = loyalty_services.record_online_purchase(
        customer.pk, order_a.pk, 500_000, 100_000, 0, at(10, 12)
    )
    adjusted = loyalty_services.adjust(
        customer.pk, "add", 2_950, "Kassada QR o‘qilmadi", staff.pk, at=at(12, 10, 3)
    )
    order_b = OrderFactory.create(
        customer=customer,
        cancelled=True,
        paid_at=at(15, 9, 3),
        created_at=at(15, 9),
        items_subtotal=300_000,
    )
    online_b = loyalty_services.record_online_purchase(customer.pk, order_b.pk, 300_000, 0, 0, at(15, 9, 3))
    reversal = loyalty_services.reverse_purchase(online_b.purchase_id, at(16, 10))
    order_c = OrderFactory.create(customer=customer, created_at=at(24, 11), cashback_used=5_000)
    loyalty_services.create_hold(customer.pk, order_c.pk, 5_000, ANCHOR + timedelta(minutes=15))
    return {
        "customer": customer,
        "store": store,
        "order_a": order_a,
        "online_a": online_a,
        "adjusted": adjusted,
        "order_b": order_b,
        "online_b": online_b,
        "reversal": reversal,
        "order_c": order_c,
        "staff": staff,
    }


def test_full_card(staff_client: APIClient, history: dict[str, Any]) -> None:
    customer: Customer = history["customer"]
    staff: StaffUser = history["staff"]
    device = DeviceFactory.create(customer=customer, last_active_at=at(24, 9, 12), push_enabled=True)
    logged_out = DeviceFactory.create(  # logged out later: never the card's device, still listed
        customer=customer,
        platform="android",
        model="Galaxy A54",
        os="Android 14",
        is_active=False,
        fcm_token="",
        push_enabled=False,
        last_active_at=at(24, 11),
    )
    rule = CustomerSegmentFactory.create(name="Bir martalik", min_online_orders=1, min_lifetime_amount=None)
    manual = CustomerSegmentFactory.create(name="Qo‘lda", min_online_orders=None, min_lifetime_amount=None)
    SegmentMemberFactory.create(segment=manual, customer=customer)
    CustomerSegmentFactory.create(name="Katta", min_online_orders=None, min_lifetime_amount=10 * MLN)
    confirmed: dict[str, Any] = {"status": enums.AppointmentStatus.CONFIRMED, "confirmed_at": at(22)}
    appointments: list[tuple[dict[str, Any], datetime]] = [
        ({"status": enums.AppointmentStatus.REQUESTED}, ANCHOR + timedelta(days=2)),
        (confirmed, ANCHOR + timedelta(days=3)),
        (confirmed, ANCHOR - timedelta(days=1)),  # past
        ({"status": enums.AppointmentStatus.REJECTED, "rejected_at": at(22)}, ANCHOR + timedelta(days=4)),
    ]
    for fields, start in appointments:
        AppointmentFactory.create(customer=customer, start=start, **fields)

    response = staff_client.get(url(customer.pk))
    assert response.status_code == 200, response.data
    data = response.json()
    entries = {
        entry.kind + ("-" + str(entry.order_id) if entry.order_id else ""): entry
        for entry in LedgerEntry.objects.filter(account__customer=customer)
    }
    order_a, order_b = history["order_a"], history["order_b"]
    credit_b = entries[f"credit-{order_b.pk}"]
    expected_ledger = [
        {
            "id": history["reversal"].credit_reversal_entry_id,
            "at": iso(at(16, 10)),
            "kind": "reversal",
            "amount": -3_000,
            "balance_after": 16_950,
            "source_type": "order",
            "source_ref": str(order_b.pk),
            "order_id": order_b.pk,
            "base": None,
            "percent": None,
            "promo_excluded": False,
            "policy_version": None,
            "reverses_id": credit_b.pk,
            "reason": None,
            "actor": None,
        },
        {
            "id": credit_b.pk,
            "at": iso(at(15, 9, 3)),
            "kind": "credit",
            "amount": 3_000,
            "balance_after": 19_950,
            "source_type": "order",
            "source_ref": str(order_b.pk),
            "order_id": order_b.pk,
            "base": 300_000,
            "percent": "1.00",
            "promo_excluded": False,
            "policy_version": 3,
            "reverses_id": None,
            "reason": None,
            "actor": None,
        },
        {
            "id": history["adjusted"].entry_id,
            "at": iso(at(12, 10, 3)),
            "kind": "adjustment",
            "amount": 2_950,
            "balance_after": 16_950,
            "source_type": "admin",
            "source_ref": None,
            "order_id": None,
            "base": None,
            "percent": None,
            "promo_excluded": False,
            "policy_version": None,
            "reverses_id": None,
            "reason": "Kassada QR o‘qilmadi",
            "actor": {"id": staff.pk, "display_name": staff.full_name or staff.email},
        },
        {
            "id": history["online_a"].credited_entry_id,
            "at": iso(at(10, 12)),
            "kind": "credit",
            "amount": 4_000,
            "balance_after": 14_000,
            "source_type": "order",
            "source_ref": str(order_a.pk),
            "order_id": order_a.pk,
            "base": 400_000,
            "percent": "1.00",
            "promo_excluded": True,
            "policy_version": 3,
            "reverses_id": None,
            "reason": None,
            "actor": None,
        },
        {
            "id": history["store"].credited_entry_id,
            "at": iso(at(5, 10)),
            "kind": "credit",
            "amount": 10_000,
            "balance_after": 10_000,
            "source_type": "billz_sale",
            "source_ref": "S-1001",
            "order_id": None,
            "base": 1_000_000,
            "percent": "1.00",
            "promo_excluded": False,
            "policy_version": 3,
            "reverses_id": None,
            "reason": None,
            "actor": None,
        },
    ]
    expected_purchases = [
        {
            "id": history["online_b"].purchase_id,
            "at": iso(at(15, 9, 3)),
            "source": "online",
            "order_id": order_b.pk,
            "billz_sale_ref": None,
            "amount": 300_000,
            "promo_amount": 0,
            "cashback": 3_000,
            "policy_version": 3,
            "reversed": True,
        },
        {
            "id": history["online_a"].purchase_id,
            "at": iso(at(10, 12)),
            "source": "online",
            "order_id": order_a.pk,
            "billz_sale_ref": None,
            "amount": 500_000,
            "promo_amount": 100_000,
            "cashback": 4_000,
            "policy_version": 3,
            "reversed": False,
        },
        {
            "id": history["store"].purchase_id,
            "at": iso(at(5, 10)),
            "source": "store_qr",
            "order_id": None,
            "billz_sale_ref": "S-1001",
            "amount": 1_000_000,
            "promo_amount": 0,
            "cashback": 10_000,
            "policy_version": 3,
            "reversed": False,
        },
    ]
    assert data == {
        "id": customer.pk,
        "name": "Malika Karimova",
        "phone": "+998901234567",
        "status": "active",
        "registered_at": iso(customer.registered_at),
        "deleted_at": None,
        "language": "uz",
        "address": "Toshkent, Chilonzor",
        "totals": {"store": 1_000_000, "online": 500_000, "lifetime": 1_500_000},
        # count: paid, non-reversed online purchases; last order: the newest order of any status (§7 item 18)
        "online_orders": {
            "count": 1,
            "last_order_id": history["order_c"].pk,
            "last_order_at": iso(at(24, 11)),
            "all_count": 3,  # every order: delivered, cancelled and awaiting payment
        },
        "tier": {
            "policy_version": 3,
            "percent": "1.00",
            "next": {"percent": "2.00", "from": 20 * MLN, "remaining": 18_500_000, "progress": 0.075},
        },
        "balance": 16_950,
        "held": 5_000,
        "available": 11_950,
        "groups": [{"id": rule.pk, "name": "Bir martalik"}, {"id": manual.pk, "name": "Qo‘lda"}],
        "upcoming_appointments": 2,
        "device": {
            "platform": "ios",
            "model": device.model,
            "os": device.os,
            "app_version": device.app_version,
            "last_active_at": iso(at(24, 9, 12)),
            "push_enabled": True,
        },
        # every installation, the active ones first; never the FCM token, the device key or the fid
        "devices": [
            {
                "id": device.pk,
                "platform": "ios",
                "model": "iPhone 14",
                "os": "iOS 18.1",
                "app_version": "1.0.0",
                "push_enabled": True,
                "is_active": True,
                "last_active_at": iso(at(24, 9, 12)),
                "created_at": iso(ANCHOR),
            },
            {
                "id": logged_out.pk,
                "platform": "android",
                "model": "Galaxy A54",
                "os": "Android 14",
                "app_version": "1.0.0",
                "push_enabled": False,
                "is_active": False,
                "last_active_at": iso(at(24, 11)),
                "created_at": iso(ANCHOR),
            },
        ],
        "ledger": expected_ledger,
        "purchases": expected_purchases,
    }


def test_card_query_count_is_bounded(staff_client: APIClient, history: dict[str, Any]) -> None:
    customer: Customer = history["customer"]
    staff_client.get(url(customer.pk))

    def count() -> int:
        with CaptureQueriesContext(connection) as context:
            assert staff_client.get(url(customer.pk)).status_code == 200
        return len(context.captured_queries)

    before = count()
    for _ in range(5):  # more rows never mean more queries
        order = OrderFactory.create(customer=customer, delivered=True, created_at=at(20))
        loyalty_services.record_online_purchase(customer.pk, order.pk, 100_000, 0, 0, at(20, 12))
    assert count() == before
    assert before <= 16
    # more segments never mean more queries either (groups: one statement, no per-segment membership probe)
    for index in range(6):
        manual = CustomerSegmentFactory.create(min_online_orders=None, min_lifetime_amount=None)
        SegmentMemberFactory.create(segment=manual, customer=customer)
        CustomerSegmentFactory.create(min_online_orders=index + 1, min_lifetime_amount=None)
        CustomerSegmentFactory.create(min_online_orders=None, min_lifetime_amount=(index + 1) * MLN)
    response = staff_client.get(url(customer.pk))
    assert len(response.json()["groups"]) == 6 + 6 + 2  # manual; ≥ 1..6 of 6 orders; ≥ 1..2 of 2.0 mln
    assert count() == before


def test_customer_without_history(staff_client: APIClient, policies: dict[int, CashbackPolicy]) -> None:
    customer = make_customer("Yangi mijoz")
    data = staff_client.get(url(customer.pk)).json()
    assert data["address"] is None and data["device"] is None
    assert data["totals"] == {"store": 0, "online": 0, "lifetime": 0}
    assert data["online_orders"] == {"count": 0, "last_order_id": None, "last_order_at": None, "all_count": 0}
    assert data["devices"] == []
    assert data["tier"] == {
        "policy_version": 3,
        "percent": "1.00",
        "next": {"percent": "2.00", "from": 20 * MLN, "remaining": 20 * MLN, "progress": 0.0},
    }
    assert (data["balance"], data["held"], data["available"]) == (0, 0, 0)
    assert data["groups"] == [] and data["upcoming_appointments"] == 0
    assert data["ledger"] == [] and data["purchases"] == []


def test_tier_is_null_without_a_policy_or_a_covering_band(
    staff_client: APIClient, clock: Any, policies: dict[int, CashbackPolicy]
) -> None:
    beyond = make_customer("Beyond")
    loyalty_services.record_store_purchase(beyond.pk, "S-9", "S-9", 50 * MLN, sold_at=at(5), at=at(5))
    top = make_customer("Top")
    loyalty_services.record_store_purchase(top.pk, "S-10", "S-10", 35 * MLN, sold_at=at(5), at=at(5))
    assert staff_client.get(url(beyond.pk)).json()["tier"] is None  # v3 stops at 50 mln
    assert staff_client.get(url(top.pk)).json()["tier"] == {
        "policy_version": 3,
        "percent": "3.00",
        "next": None,
    }


def test_tier_is_null_before_any_policy(staff_client: APIClient, clock: Any) -> None:
    customer = make_customer("Mijoz")
    assert staff_client.get(url(customer.pk)).json()["tier"] is None


def test_deleted_customer_renders_scrubbed(
    staff_client: APIClient, staff: StaffUser, policies: dict[int, CashbackPolicy]
) -> None:
    customer = make_customer("Eski mijoz")
    loyalty_services.record_store_purchase(customer.pk, "S-77", "S-77", 400_000, sold_at=at(5), at=at(5))
    segment = CustomerSegmentFactory.create(min_online_orders=None, min_lifetime_amount=None)
    SegmentMemberFactory.create(segment=segment, customer=customer)
    Customer.objects.filter(pk=customer.pk).update(
        name="", status=enums.CustomerStatus.DELETED, deleted_at=at(20, 9), address_text=""
    )
    data = staff_client.get(url(customer.pk)).json()
    assert data["name"] == "" and data["status"] == "deleted"
    assert data["deleted_at"] == iso(at(20, 9))
    assert data["address"] is None
    assert data["groups"] == []  # membership needs an active customer
    assert data["balance"] == 4_000 and len(data["ledger"]) == 1  # kept for a re-registration
    assert data["totals"]["lifetime"] == 400_000


def test_devices_never_carry_secrets_and_are_capped(staff_client: APIClient, clock: Any) -> None:
    customer = make_customer("Ko‘p qurilma")
    for day in range(1, 26):
        DeviceFactory.create(
            customer=customer,
            fid=f"fid-many-{day:02d}",
            is_active=day == 3,
            fcm_token=f"fcm-many-{day}" if day == 3 else "",
            push_enabled=day == 3,
            key_hash="k" * 64,
            last_active_at=at(day),
        )
    data = staff_client.get(url(customer.pk)).json()
    devices = data["devices"]
    assert len(devices) == 20  # CARD_DEVICES_LIMIT
    assert devices[0]["is_active"] is True and devices[0]["last_active_at"] == iso(at(3))
    assert [row["last_active_at"] for row in devices[1:]] == [iso(at(day)) for day in range(25, 6, -1)]
    assert data["device"]["last_active_at"] == iso(at(3))
    body = str(data)
    assert "fcm-many" not in body and "fid-many" not in body and "k" * 64 not in body


def test_a_deleted_customer_keeps_the_device_history(staff_client: APIClient, clock: Any) -> None:
    customer = make_customer("Ketgan mijoz")
    DeviceFactory.create(customer=customer, last_active_at=at(20))
    accounts_services.delete_customer(customer)
    data = staff_client.get(url(customer.pk)).json()
    assert data["device"] is None
    assert [(row["is_active"], row["push_enabled"]) for row in data["devices"]] == [(False, False)]


def test_unknown_customer_is_404(staff_client: APIClient, clock: Any) -> None:
    response = staff_client.get(url(999_999))
    assert response.status_code == 404
    assert response.data == {
        "code": "not_found",
        "message": "Topilmadi.",
        "errors": [],
        "request_id": response.data["request_id"],
    }
