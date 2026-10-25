"""delivery.services / selectors (design 02 §3.1): shipment transitions, derived ``pending``, BTS mapping."""

from collections.abc import Iterator
from datetime import datetime

import pytest

from glowderma.core import enums
from glowderma.core.models import OutboxMessage
from glowderma.core.seeding import pinned_clock
from glowderma.core.timeutils import TASHKENT
from glowderma.delivery import bts, selectors, services
from glowderma.delivery.models import Shipment, ShipmentEvent
from glowderma.delivery.tests.factories import OrderRowFactory, ShipmentFactory
from glowderma.integrations.bts import fake as fake_bts
from glowderma.integrations.bts.ports import BtsUnavailable

pytestmark = pytest.mark.django_db
AT = datetime(2026, 10, 23, 14, 6, tzinfo=TASHKENT)


@pytest.fixture(autouse=True)
def clean_fake() -> Iterator[None]:
    fake_bts.reset()
    yield
    fake_bts.reset()


def test_ensure_and_request_create() -> None:
    order = OrderRowFactory.create(paid=True)
    shipment_id = services.ensure_shipment(order.pk)
    assert services.ensure_shipment(order.pk) == shipment_id
    message_id = services.request_bts_create(order.pk, manual=False, actor_id=None)
    message = OutboxMessage.objects.get(pk=message_id)
    assert (message.topic, message.dedupe_key) == ("bts.create_shipment", f"bts.create_shipment:{order.pk}:1")
    info = selectors.shipment_info(order.pk)
    assert info is not None and (info.state, info.pending, info.create_requests) == ("not_created", True, 1)
    assert selectors.shipment_for_order(order.pk, "delivery") == {"state": "not_created", "pending": True}
    OutboxMessage.objects.filter(pk=message_id).update(status="done")
    assert selectors.shipment_for_order(order.pk, "delivery") == {"state": "not_created", "pending": False}
    with pytest.raises(services.ShipmentMissing):
        services.request_bts_create(OrderRowFactory.create().pk, manual=True, actor_id=None)


def test_mark_failed_then_created_then_status_and_cancel() -> None:
    order = OrderRowFactory.create(paid=True)
    services.ensure_shipment(order.pk)
    assert (
        services.mark_failed(order.pk, error_code="address_not_found", error="Manzil topilmadi", at=AT) == 1
    )
    view = selectors.shipment_for_order(order.pk, "delivery")
    assert view == {"state": "failed", "last_attempt_at": AT, "attempts": 1, "pending": False}
    assert services.mark_created(
        order.pk, tracking_number="48213907", tariff=25_000, bts_order_id="BTS-1", bts_status="100", at=AT
    )
    assert not services.mark_created(
        order.pk, tracking_number="X", tariff=1, bts_order_id="", bts_status="100", at=AT
    )
    assert services.mark_failed(order.pk, error_code="late", at=AT) is None  # parcel exists: nothing written
    shipment = Shipment.objects.get(order=order)
    assert (shipment.state, shipment.attempts, shipment.via, shipment.last_error_code) == (
        "created",
        2,
        "polling",
        "",
    )
    assert selectors.shipment_for_order(order.pk, "delivery") == {
        "state": "created",
        "tracking_number": "48213907",
        "tariff": 25_000,
        "updated_at": AT,
        "via": "polling",
    }
    with pinned_clock(AT):
        result = services.apply_status(order.pk, bts_status="200", via="webhook", payload={"x": 1})
        duplicate = services.apply_status(order.pk, bts_status="200", via="webhook", payload={"x": 1})
    assert (result.changed, result.target_status) == (True, "handed_to_courier")
    assert (duplicate.changed, duplicate.target_status) == (False, "handed_to_courier")
    refused = services.cancel(order.pk)
    assert (refused.ok, refused.reason) == (False, "posilka kuryerda")
    assert not ShipmentEvent.objects.filter(kind="cancelled").exists()
    with pytest.raises(BtsUnavailable):
        services.cancel(order.pk, scenario={"cancel": "unavailable"})
    ok = services.cancel(order.pk, scenario={"status": "100"}, at=AT)
    assert ok.ok and Shipment.objects.get(order=order).state == "cancelled"
    assert selectors.shipment_for_order(order.pk, "delivery")["state"] == "created"  # D-1
    with pytest.raises(services.DeliveryError):
        services.cancel(order.pk)
    late = services.apply_status(order.pk, bts_status="300", via="polling")  # recorded, the order ignores it
    assert late.changed and Shipment.objects.get(order=order).state == "cancelled"
    kinds = list(
        ShipmentEvent.objects.filter(shipment=shipment).order_by("id").values_list("kind", flat=True)
    )
    assert kinds == ["create_failed", "created", "status_received", "cancelled", "status_received"]
    with pytest.raises(services.DeliveryError):
        services.apply_status(OrderRowFactory.create(paid=True).pk, bts_status="200", via="webhook")
    other = OrderRowFactory.create(paid=True)
    services.ensure_shipment(other.pk)
    with pytest.raises(services.DeliveryError):
        services.apply_status(other.pk, bts_status="200", via="webhook")


def test_views_and_attention_count() -> None:
    assert selectors.shipment_view(None, "pickup") == {"state": "not_required"}
    assert selectors.shipment_view(None, "delivery") == {"state": "not_created", "pending": False}
    assert selectors.shipment_info(OrderRowFactory.create().pk) is None
    failed = ShipmentFactory.create(failed=True, order=OrderRowFactory.create(paid=True))
    ShipmentFactory.create(failed=True, order=OrderRowFactory.create(cancelled=True))
    assert selectors.bts_failed_count() == 1
    assert selectors.api_state(failed, "delivery") == enums.ShipmentState.FAILED


def test_bts_status_mapping() -> None:
    assert [bts.map_status(code) for code in ("100", "200", "300", "999")] == [
        None,
        "handed_to_courier",
        "delivered",
        None,
    ]
    assert bts.is_cancellable("") and bts.is_cancellable("100") and not bts.is_cancellable("200")
