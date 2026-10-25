"""Admin API — contract §2.2 orders: list (filters, search, period, ordering, paging, export), status counts,
detail shape, cancel / transition / retry-shipment (codes, messages, idempotency)."""

import uuid
from datetime import timedelta
from typing import Any

import pytest
from rest_framework.test import APIClient

from glowderma.commerce import services, topics
from glowderma.commerce.models import Order
from glowderma.commerce.tests.support import AT, at, deliver, pay, place, product, ship, shopper
from glowderma.core.models import AuditEntry, IdempotencyRecord
from glowderma.core.seeding import pinned_clock
from glowderma.payments.models import Refund

pytestmark = pytest.mark.django_db
URL = "/api/v1/admin/orders/"


def _ids(response: Any) -> list[int]:
    assert response.status_code == 200, response.json()
    return [row["id"] for row in response.json()["results"]]


def _post(client: APIClient, path: str, body: Any = None, key: str | None = None) -> Any:
    return client.post(path, body or {}, format="json", HTTP_IDEMPOTENCY_KEY=key or str(uuid.uuid4()))


# --- list ---------------------------------------------------------------------------------------------------


def test_list_shape_ordering_and_paging(staff_client: APIClient) -> None:
    orders = [place(when=AT + timedelta(minutes=i)) for i in range(3)]
    same = place(when=AT + timedelta(minutes=2))  # tie on created_at → id breaks it
    with pinned_clock(AT + timedelta(hours=1)):
        response = staff_client.get(URL, {"page_size": 6})
    body = response.json()
    assert (body["count"], body["page"], body["page_size"], body["page_count"]) == (4, 1, 6, 1)
    assert [row["id"] for row in body["results"]] == [same.pk, orders[2].pk, orders[1].pk, orders[0].pk]
    row = body["results"][-1]
    assert row == {
        "id": orders[0].pk,
        "customer": {
            "id": orders[0].customer_id,
            "name": orders[0].customer.name,
            "phone": orders[0].customer.phone,
        },
        "created_at": "2026-10-20T12:00:00+05:00",
        "total": orders[0].total,
        "status": "awaiting_payment",
        "payment": {"status": "pending", "provider": "click"},
        "fulfilment": "delivery",
        "shipment": {"state": "not_created", "pending": False},
        "cancellation": None,
    }
    assert _ids(staff_client.get(URL, {"ordering": "created_at"}))[:2] == [orders[0].pk, orders[1].pk]
    assert staff_client.get(URL).json()["page_size"] == 10
    bad = staff_client.get(URL, {"page_size": 20})
    assert bad.status_code == 400 and bad.json()["errors"][0] == {
        "field": "page_size",
        "code": "invalid_choice",
        "message": "Ruxsat etilmagan qiymat.",
    }


def test_filters_status_fulfilment_payment_customer(staff_client: APIClient) -> None:
    customer = shopper()
    waiting = place(customer)
    pickup = pay(place(fulfilment="pickup"))
    shipped = ship(place())
    assert _ids(staff_client.get(URL, {"status": "processing"})) == [shipped.pk, pickup.pk]
    assert _ids(staff_client.get(URL, {"fulfilment": "pickup"})) == [pickup.pk]
    assert _ids(staff_client.get(URL, {"payment": "paid"})) == [shipped.pk, pickup.pk]
    assert _ids(staff_client.get(URL, {"payment": "pending"})) == [waiting.pk]
    assert _ids(staff_client.get(URL, {"customer": customer.pk})) == [waiting.pk]
    response = staff_client.get(URL, {"status": "lost"})
    assert response.status_code == 400 and response.json()["errors"][0]["field"] == "status"


def test_search_rule(staff_client: APIClient) -> None:
    malika = shopper(name="Malika Karimova", phone="+998901234567")
    other = shopper(name="Sardor Umarov", phone="+998993007215")
    first = place(malika)
    second = place(other)
    assert _ids(staff_client.get(URL, {"search": f"№{first.pk}"})) == [first.pk]
    assert _ids(staff_client.get(URL, {"search": str(second.pk)[-3:]})) == [second.pk]
    assert _ids(staff_client.get(URL, {"search": "1234"})) == [first.pk]  # ≥ 3 digits → phone
    assert _ids(staff_client.get(URL, {"search": "  malika "})) == [
        first.pk
    ]  # name, trimmed, case-insensitive
    assert _ids(staff_client.get(URL, {"search": "umarov"})) == [second.pk]
    assert _ids(staff_client.get(URL, {"search": "zz"})) == []
    assert staff_client.get(URL, {"search": "x" * 65}).status_code == 400


def test_period_and_dates_use_tashkent_days(staff_client: APIClient) -> None:
    last_second = place(when=at(19, 23, 59, 59))
    midnight = place(when=at(20, 0, 0, 0))
    early_month = place(when=at(1, 9))
    old = place(when=at(12, 9))
    with pinned_clock(at(20, 10)):
        assert _ids(staff_client.get(URL, {"period": "today"})) == [midnight.pk]
        assert set(_ids(staff_client.get(URL, {"period": "week"}))) == {midnight.pk, last_second.pk}
        assert set(_ids(staff_client.get(URL, {"period": "month"}))) == {
            midnight.pk,
            last_second.pk,
            early_month.pk,
            old.pk,
        }
        assert len(_ids(staff_client.get(URL, {"period": "all"}))) == 4
        assert _ids(staff_client.get(URL, {"date_from": "2026-10-19", "date_to": "2026-10-19"})) == [
            last_second.pk
        ]
        assert _ids(staff_client.get(URL, {"date_from": "2026-10-20"})) == [midnight.pk]
        assert _ids(staff_client.get(URL, {"period": "today", "date_to": "2026-10-19"})) == []  # ANDed
        bad = staff_client.get(URL, {"date_from": "2026-10-21", "date_to": "2026-10-20"})
    assert bad.status_code == 400 and bad.json()["errors"][0]["field"] == "date_from"


def test_status_counts_are_consistent_with_the_list(staff_client: APIClient) -> None:
    customer = shopper()
    place(customer)
    pay(place(customer, fulfilment="pickup"))
    place(when=at(1))
    with pinned_clock(at(20, 13)):
        counts = staff_client.get(URL + "status-counts/", {"customer": customer.pk}).json()
        assert counts == {
            "all": 2,
            "awaiting_payment": 1,
            "processing": 1,
            "handed_to_courier": 0,
            "delivered": 0,
            "cancelled": 0,
        }
        assert staff_client.get(URL + "status-counts/").json()["all"] == 3
        for status in ("awaiting_payment", "processing"):
            listed = staff_client.get(URL, {"status": status, "period": "today"}).json()["count"]
            assert staff_client.get(URL + "status-counts/", {"period": "today"}).json()[status] == listed


def test_export_csv_and_xlsx(staff_client: APIClient) -> None:
    order = ship(place(shopper(name="=Hack", phone="+998901112233")))
    response = staff_client.get(URL, {"export": "csv", "status": "processing"})
    assert response.status_code == 200
    assert response["Content-Disposition"].startswith('attachment; filename="buyurtmalar-')
    text = response.content.decode("utf-8-sig")
    header, line = text.split("\r\n")[:2]
    assert header == '"№";"Mijoz";"Telefon";"Sana";"Summa (so‘m)";"To‘lov";"Usul";"Olish usuli";"Holat";"BTS"'
    tracking = order.shipment.tracking_number
    assert line == (
        f'"{order.pk}";"\'=Hack";"+998901112233";"2026-10-20 12:00";"{order.total}";"To‘langan";"Click";'
        f'"Yetkazib berish";"Jarayonda";"{tracking}"'
    )
    xlsx = staff_client.get(URL, {"export": "xlsx"})
    assert xlsx.status_code == 200 and xlsx.content[:2] == b"PK"
    audit = AuditEntry.objects.filter(action="export.downloaded").first()
    assert audit is not None and audit.after["resource"] == "orders"  # type: ignore[index]


# --- detail -------------------------------------------------------------------------------------------------


def test_detail_404_and_shape_of_a_cancelled_refunded_order(staff_client: APIClient) -> None:
    assert staff_client.get(URL + "999999/").status_code == 404
    order = ship(place(fulfilment="delivery"))
    with pinned_clock(AT + timedelta(minutes=60)):
        response = _post(staff_client, f"{URL}{order.pk}/cancel/", {"reason": "Mijoz so‘rovi bilan"})
    assert response.status_code == 200, response.json()
    body = response.json()
    refund = Refund.objects.get()
    assert body["status"] == "cancelled"
    assert body["cancellation"] == {"reason": "admin", "note": "Mijoz so‘rovi bilan"}
    assert body["refunds"] == [
        {
            "id": refund.pk,
            "state": "requested",
            "provider": "click",
            "amount": order.total,
            "requested_at": "2026-10-20T13:00:00+05:00",
            "updated_at": "2026-10-20T13:00:00+05:00",
            "completed_at": None,
        }
    ]
    assert (
        body["payment"]["status"] == "paid" and body["payment"]["confirmed_at"] == "2026-10-20T12:03:00+05:00"
    )
    assert body["shipment"]["state"] == "created"  # a cancelled parcel renders as created (D-1)
    assert body["available_actions"] == {"transitions": [], "cancel": False, "retry_shipment": False}
    assert [event["type"] for event in body["history"]][:2] == ["refund_requested", "cancelled"]
    cancelled = body["history"][1]
    assert cancelled["actor"]["display_name"] and cancelled["source"] == "admin"
    assert body["cashback"] == [] and body["cashback_hold"] is None
    with pinned_clock(AT + timedelta(minutes=62)):
        deliver(topics.REFUND_SUBMIT, order.pk)
    detail = staff_client.get(f"{URL}{order.pk}/").json()
    assert detail["payment"]["status"] == "refunded" and detail["refunds"][0]["state"] == "succeeded"
    assert detail["refunds"][0]["completed_at"] == "2026-10-20T13:02:00+05:00"
    assert detail["history"][0]["type"] == "refund_succeeded"


def test_detail_of_an_open_order_shows_the_active_hold_and_items(staff_client: APIClient) -> None:
    from glowderma.commerce.tests.support import credit, seed_policies

    seed_policies()
    customer = shopper()
    credit(customer, 40_000, AT - timedelta(days=1))
    item = product(150_000, image=True)
    order = place(customer, [(item, 2)], fulfilment="pickup", cashback=30_000)
    body = staff_client.get(f"{URL}{order.pk}/").json()
    assert body["address"] is None and body["channel"] == "ios"
    assert body["items"] == [
        {
            "product_id": item.pk,
            "name": item.name_uz,
            "billz_id": item.billz_id,
            "image_url": order.items.get().image_url,
            "quantity": 2,
            "unit_price": 150_000,
            "promo": False,
        }
    ]
    assert (body["items_subtotal"], body["promo_subtotal"], body["delivery_fee"], body["cashback_used"]) == (
        300_000,
        0,
        0,
        30_000,
    )
    assert body["cashback_hold"] == {
        "amount": 30_000,
        "state": "active",
        "expires_at": "2026-10-20T12:15:00+05:00",
    }
    assert body["payment"] == {
        "status": "pending",
        "provider": "click",
        "amount": 270_000,
        "transaction_id": None,
        "confirmed_at": None,
    }
    assert body["shipment"] == {"state": "not_required"}
    assert body["available_actions"] == {"transitions": [], "cancel": True, "retry_shipment": False}
    pay(order)
    paid = staff_client.get(f"{URL}{order.pk}/").json()
    assert paid["cashback_hold"] is None
    assert [(e["kind"], e["amount"], e["base"], e["percent"]) for e in paid["cashback"]] == [
        ("debit", -30_000, None, None),
        ("credit", 3_000, 300_000, "1.00"),
    ]
    assert paid["available_actions"]["transitions"] == ["delivered"]


# --- actions ------------------------------------------------------------------------------------------------


def test_cancel_validation_and_codes(staff_client: APIClient) -> None:
    order = place()
    path = f"{URL}{order.pk}/cancel/"
    missing_key = staff_client.post(path, {"reason": "ok reason"}, format="json")
    assert missing_key.status_code == 400 and missing_key.json()["code"] == "idempotency_key_required"
    short = _post(staff_client, path, {"reason": "  ab  "})
    assert short.status_code == 400
    assert short.json()["message"] == "Sababni kiriting."
    assert short.json()["errors"] == [
        {"field": "reason", "code": "min_length", "message": "Kamida 3 ta belgi kiriting."}
    ]
    long = _post(staff_client, path, {"reason": "x" * 501})
    assert long.json()["errors"][0] == {
        "field": "reason",
        "code": "max_length",
        "message": "500 belgidan oshmasin.",
    }
    assert _post(staff_client, path, {}).json()["errors"][0]["code"] == "required"
    assert _post(staff_client, f"{URL}999999/cancel/", {"reason": "abc"}).status_code == 404
    assert _post(staff_client, path, {"reason": "Mijoz so‘rovi bilan"}).status_code == 200
    again = _post(staff_client, path, {"reason": "Mijoz so‘rovi bilan"})
    assert again.status_code == 409
    assert (
        again.json()["code"] == "not_cancellable"
        and again.json()["message"] == "Bu buyurtmani bekor qilib bo‘lmaydi."
    )


def test_cancel_bts_refusal_and_outage_over_http(staff_client: APIClient) -> None:
    refused = ship(place())
    with pinned_clock(AT + timedelta(hours=2)):
        services.apply_bts_status(refused.pk, "200", "webhook")
    response = _post(staff_client, f"{URL}{refused.pk}/cancel/", {"reason": "Mijoz so‘rovi bilan"})
    assert response.status_code == 409 and response.json() == {
        "code": "not_cancellable",
        "message": "BTS buyurtmani bekor qilmadi: posilka kuryerda.",
        "errors": [],
        "request_id": response.json()["request_id"],
    }
    down = ship(place(fake={"bts": {"cancel": "unavailable"}}))
    key = str(uuid.uuid4())
    outage = _post(staff_client, f"{URL}{down.pk}/cancel/", {"reason": "Mijoz so‘rovi bilan"}, key)
    assert outage.status_code == 503 and outage.json()["code"] == "provider_unavailable"
    assert not IdempotencyRecord.objects.exists()  # an error stores nothing: the same key may retry
    assert Order.objects.get(pk=down.pk).status == "processing"


def test_idempotent_replay_and_key_reuse(staff_client: APIClient) -> None:
    order = place()
    key = str(uuid.uuid4())
    path = f"{URL}{order.pk}/cancel/"
    first = _post(staff_client, path, {"reason": "Mijoz so‘rovi bilan"}, key)
    replay = _post(staff_client, path, {"reason": "Mijoz so‘rovi bilan"}, key)
    assert (first.status_code, replay.status_code) == (200, 200)
    assert replay.json() == first.json() and replay["Idempotent-Replayed"] == "true"
    reused = _post(staff_client, path, {"reason": "boshqa sabab"}, key)
    assert reused.status_code == 422 and reused.json()["code"] == "idempotency_key_reused"
    assert AuditEntry.objects.filter(action="order.cancelled").count() == 1


def test_transition_endpoint(staff_client: APIClient) -> None:
    order = pay(place(fulfilment="pickup"))
    path = f"{URL}{order.pk}/transition/"
    wrong = _post(staff_client, path, {"status": "handed_to_courier"})
    assert wrong.status_code == 409 and wrong.json()["code"] == "invalid_transition"
    assert wrong.json()["message"] == "Bu holatga o‘tib bo‘lmaydi."
    assert _post(staff_client, path, {"status": "teleported"}).json()["errors"][0]["code"] == "invalid_choice"
    key = str(uuid.uuid4())
    done = _post(staff_client, path, {"status": "delivered"}, key)
    assert done.status_code == 200 and done.json()["status"] == "delivered"
    assert done.json()["history"][0]["type"] == "status_changed"
    assert _post(staff_client, path, {"status": "delivered"}, key).json() == done.json()  # replay
    assert _post(staff_client, f"{URL}999999/transition/", {"status": "delivered"}).status_code == 404


def test_retry_shipment_endpoint(staff_client: APIClient) -> None:
    order = pay(place(fake={"bts": {"fail_attempts": 1}}))
    path = f"{URL}{order.pk}/retry-shipment/"
    early = _post(staff_client, path)
    assert early.status_code == 409 and early.json()["code"] == "invalid_transition"
    assert early.json()["message"] == "BTS ga qayta yuborib bo‘lmaydi."
    deliver(topics.BTS_CREATE_SHIPMENT, order.pk)
    assert staff_client.get(f"{URL}{order.pk}/").json()["shipment"]["state"] == "failed"
    key = str(uuid.uuid4())
    accepted = _post(staff_client, path, None, key)
    assert accepted.status_code == 202
    assert accepted.json()["shipment"] == {
        "state": "failed",
        "last_attempt_at": accepted.json()["shipment"]["last_attempt_at"],
        "attempts": 1,
        "pending": True,
    }
    assert accepted.json()["available_actions"]["retry_shipment"] is False
    replay = _post(staff_client, path, None, key)
    assert replay.status_code == 202 and replay["Idempotent-Replayed"] == "true"
    busy = _post(staff_client, path)
    assert busy.status_code == 409 and busy.json() == {
        "code": "conflict",
        "message": "BTS so‘rovi allaqachon yuborilmoqda.",
        "errors": [],
        "request_id": busy.json()["request_id"],
    }
    assert _post(staff_client, f"{URL}999999/retry-shipment/").status_code == 404
