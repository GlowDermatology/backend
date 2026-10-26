"""The customer inbox (customer contract §2.12; plan §4 P3): feed + unread badge, read one / all, delivery
acknowledgements and the admin ``Campaign.delivered`` counter."""

import threading
from datetime import timedelta
from typing import Any

import pytest
from django.db import connection
from rest_framework.test import APIClient

from glowderma.accounts.models import Customer
from glowderma.accounts.tests.factories import CustomerFactory
from glowderma.core import timeutils
from glowderma.notifications import services
from glowderma.notifications.models import Campaign, CampaignRecipient, InboxNotification
from glowderma.notifications.tests.factories import (
    CampaignRecipientFactory,
    InboxNotificationFactory,
    SentCampaignFactory,
)

pytestmark = pytest.mark.django_db

FEED = "/api/v1/me/notifications/"
UNREAD = "/api/v1/me/notifications/unread-count/"
READ_ALL = "/api/v1/me/notifications/read-all/"
ACK = "/api/v1/me/notifications/ack/"


def read_url(pk: int) -> str:
    return f"/api/v1/me/notifications/{pk}/read/"


def campaign_row(customer: Customer, campaign: Campaign | None = None) -> InboxNotification:
    """A campaign inbox row with its recipient (the shape ``send_batch`` leaves behind)."""
    campaign = campaign or SentCampaignFactory.create()
    inbox = InboxNotificationFactory.create(
        customer=customer, source_id=campaign.pk, dedupe_key=f"campaign:{campaign.pk}:{customer.pk}"
    )
    CampaignRecipientFactory.create(campaign=campaign, customer=customer, inbox=inbox)
    return inbox


def order_row(customer: Customer, order_id: int = 10248, **extra: Any) -> InboxNotification:
    return InboxNotificationFactory.create(
        customer=customer,
        kind="order_status",
        title="Kuryerga topshirildi",
        body=f"Buyurtma №{order_id} kuryerga topshirildi.",
        link={"kind": "order", "order_id": order_id},
        source_type="order",
        source_id=order_id,
        template_status="handed_to_courier",
        dedupe_key=f"order_status:{order_id}:handed_to_courier",
        **extra,
    )


def test_feed_newest_first_with_unread_count_and_link_shape(
    customer: Customer, customer_client: APIClient
) -> None:
    older = order_row(customer)
    InboxNotification.objects.filter(pk=older.pk).update(created_at=timeutils.now() - timedelta(hours=2))
    newer = campaign_row(customer)
    InboxNotification.objects.filter(pk=newer.pk).update(read_at=timeutils.now())
    InboxNotificationFactory.create()  # another customer's row
    response = customer_client.get(FEED)
    assert response.status_code == 200
    body = response.json()
    assert (body["count"], body["page"], body["page_size"], body["page_count"], body["unread_count"]) == (
        2,
        1,
        20,
        1,
        1,
    )
    assert [row["id"] for row in body["results"]] == [newer.pk, older.pk]
    item = body["results"][1]
    assert item == {
        "id": older.pk,
        "kind": "order_status",
        "title": "Kuryerga topshirildi",
        "body": "Buyurtma №10248 kuryerga topshirildi.",
        "image_url": None,
        "link": {"kind": "order", "product_id": None, "order_id": 10248, "appointment_id": None},
        "created_at": item["created_at"],
        "read_at": None,
        "delivered_at": None,
    }
    assert body["results"][0]["link"] == {
        "kind": "none",
        "product_id": None,
        "order_id": None,
        "appointment_id": None,
    }
    assert customer_client.get(FEED, {"page_size": 50}).json()["page_size"] == 50
    assert customer_client.get(FEED, {"page_size": 30}).status_code == 400


def test_feed_pagination(customer: Customer, customer_client: APIClient) -> None:
    for order_id in range(25):
        order_row(customer, order_id=10_000 + order_id)
    first = customer_client.get(FEED).json()
    second = customer_client.get(FEED, {"page": 2}).json()
    assert (first["count"], first["page_count"], len(first["results"]), len(second["results"])) == (
        25,
        2,
        20,
        5,
    )
    assert first["unread_count"] == second["unread_count"] == 25
    assert customer_client.get(UNREAD).json() == {"unread_count": 25}


def test_history_horizon_hides_older_rows_everywhere(customer: Customer, customer_client: APIClient) -> None:
    old = order_row(customer, order_id=1)
    InboxNotification.objects.filter(pk=old.pk).update(created_at=timeutils.now() - timedelta(days=30))
    fresh = order_row(customer, order_id=2)
    Customer.objects.filter(pk=customer.pk).update(history_from=timeutils.now() - timedelta(days=1))
    body = customer_client.get(FEED).json()
    assert [row["id"] for row in body["results"]] == [fresh.pk] and body["unread_count"] == 1
    assert customer_client.get(UNREAD).json() == {"unread_count": 1}
    assert customer_client.post(read_url(old.pk)).status_code == 404
    assert customer_client.post(ACK, {"inbox_ids": [old.pk]}, format="json").json() == {"acknowledged": 0}
    assert customer_client.post(READ_ALL).json() == {"updated": 1, "unread_count": 0}
    old.refresh_from_db()
    assert old.read_at is None and old.delivered_at is None


def test_read_one_is_idempotent_and_counts_the_delivery(
    customer: Customer, customer_client: APIClient
) -> None:
    row = campaign_row(customer)
    order_row(customer)
    response = customer_client.post(read_url(row.pk))
    assert response.status_code == 200
    body = response.json()
    row.refresh_from_db()
    assert body == {"id": row.pk, "read_at": body["read_at"], "unread_count": 1}
    assert row.read_at is not None and row.delivered_at is not None
    campaign = Campaign.objects.get(pk=row.source_id)
    assert campaign.delivered == 1
    assert CampaignRecipient.objects.get(inbox=row).delivered_at is not None
    again = customer_client.post(read_url(row.pk)).json()
    assert again == body
    campaign.refresh_from_db()
    assert campaign.delivered == 1
    # read before ack counts once: the ack that arrives later changes nothing
    assert customer_client.post(ACK, {"inbox_ids": [row.pk]}, format="json").json() == {"acknowledged": 1}
    campaign.refresh_from_db()
    assert campaign.delivered == 1


def test_read_one_of_another_customer_is_404(customer_client: APIClient) -> None:
    foreign = InboxNotificationFactory.create()
    response = customer_client.post(read_url(foreign.pk))
    assert response.status_code == 404 and response.json()["code"] == "not_found"
    assert customer_client.post(read_url(999_999)).status_code == 404
    foreign.refresh_from_db()
    assert foreign.read_at is None and foreign.delivered_at is None


def test_read_all_acknowledges_every_row(customer: Customer, customer_client: APIClient) -> None:
    campaign = SentCampaignFactory.create()
    rows = [campaign_row(customer, campaign), order_row(customer)]
    already = order_row(customer, order_id=9)
    InboxNotification.objects.filter(pk=already.pk).update(read_at=timeutils.now())
    foreign = InboxNotificationFactory.create()
    assert customer_client.post(READ_ALL).json() == {"updated": 2, "unread_count": 0}
    for row in rows:
        row.refresh_from_db()
        assert row.read_at is not None and row.delivered_at is not None
    campaign.refresh_from_db()
    assert campaign.delivered == 1
    foreign.refresh_from_db()
    assert foreign.read_at is None
    assert customer_client.post(READ_ALL).json() == {"updated": 0, "unread_count": 0}


def test_ack_fills_the_delivered_counter_once(customer: Customer, customer_client: APIClient) -> None:
    campaign = SentCampaignFactory.create()
    assert Campaign.objects.get(pk=campaign.pk).delivered is None  # M1: null until the first ack
    mine = campaign_row(customer, campaign)
    other_customer = CustomerFactory.create()
    theirs = campaign_row(other_customer, campaign)
    response = customer_client.post(ACK, {"inbox_ids": [mine.pk, theirs.pk, 999_999]}, format="json")
    assert response.status_code == 200 and response.json() == {"acknowledged": 1}
    campaign.refresh_from_db()
    assert campaign.delivered == 1
    mine.refresh_from_db()
    theirs.refresh_from_db()
    assert mine.delivered_at is not None and mine.read_at is None and theirs.delivered_at is None
    retried = customer_client.post(ACK, {"inbox_ids": [mine.pk]}, format="json")
    assert retried.json() == {"acknowledged": 1}
    campaign.refresh_from_db()
    assert campaign.delivered == 1
    # the other customer's ack adds its own delivery
    services.acknowledge(other_customer.pk, [theirs.pk])
    campaign.refresh_from_db()
    assert campaign.delivered == 2


def test_ack_of_a_status_push_has_no_campaign(customer: Customer, customer_client: APIClient) -> None:
    row = order_row(customer)
    assert customer_client.post(ACK, {"inbox_ids": [row.pk]}, format="json").json() == {"acknowledged": 1}
    row.refresh_from_db()
    assert row.delivered_at is not None


@pytest.mark.parametrize(
    ("body", "code", "message"),
    [
        ({}, "required", "Majburiy maydon."),
        ({"inbox_ids": []}, "required", "Majburiy maydon."),
        ({"inbox_ids": list(range(1, 52))}, "max_items", "Ko‘pi bilan 50 ta."),
        ({"inbox_ids": ["x"]}, "invalid", "Noto‘g‘ri qiymat."),
    ],
)
def test_ack_validation(customer_client: APIClient, body: dict[str, Any], code: str, message: str) -> None:
    response = customer_client.post(ACK, body, format="json")
    assert response.status_code == 400
    error = response.json()["errors"][0]
    assert error["field"].startswith("inbox_ids") and (error["code"], error["message"]) == (code, message)


def test_ack_ignores_ids_below_one_like_unknown_ids(customer: Customer, customer_client: APIClient) -> None:
    row = order_row(customer)
    response = customer_client.post(ACK, {"inbox_ids": [0, -5, row.pk]}, format="json")
    assert response.status_code == 200 and response.json() == {"acknowledged": 1}
    row.refresh_from_db()
    assert row.delivered_at is not None
    assert services.acknowledge(customer.pk, [0, -1]).acknowledged == 0


def test_inbox_writes_document_the_optional_idempotency_key() -> None:
    from drf_spectacular.generators import SchemaGenerator

    schema = SchemaGenerator().get_schema(request=None, public=True)  # type: ignore[no-untyped-call]
    operations = {
        operation["operationId"]: operation
        for path in schema["paths"].values()
        for operation in path.values()
        if isinstance(operation, dict) and "operationId" in operation
    }
    for operation_id in (
        "customer_notifications_read",
        "customer_notifications_read_all",
        "customer_notifications_ack",
    ):
        [header] = [p for p in operations[operation_id]["parameters"] if p["name"] == "Idempotency-Key"]
        assert header["in"] == "header" and header.get("required", False) is False


@pytest.mark.django_db(transaction=True)
def test_concurrent_acks_count_once() -> None:
    customer = CustomerFactory.create()
    row = campaign_row(customer)
    start = threading.Event()
    errors: list[BaseException] = []

    def ack() -> None:
        try:
            start.wait(10)
            services.acknowledge(customer.pk, [row.pk])
        except BaseException as exc:  # noqa: BLE001 - reported below
            errors.append(exc)
        finally:
            connection.close()

    threads = [threading.Thread(target=ack) for _ in range(3)]
    for thread in threads:
        thread.start()
    start.set()
    for thread in threads:
        thread.join(10)
    assert not errors
    assert Campaign.objects.get(pk=row.source_id).delivered == 1


def test_an_optional_idempotency_key_replays_the_ack(customer: Customer, customer_client: APIClient) -> None:
    row = order_row(customer)
    key = "6f1c2a0e-9b8d-4c37-8f00-000000000001"
    first = customer_client.post(ACK, {"inbox_ids": [row.pk]}, format="json", HTTP_IDEMPOTENCY_KEY=key)
    again = customer_client.post(ACK, {"inbox_ids": [row.pk]}, format="json", HTTP_IDEMPOTENCY_KEY=key)
    assert first.json() == again.json() == {"acknowledged": 1}
    assert again["Idempotent-Replayed"] == "true"
    assert customer_client.post(read_url(row.pk), HTTP_IDEMPOTENCY_KEY=key).status_code == 200
