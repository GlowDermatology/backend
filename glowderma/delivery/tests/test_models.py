"""delivery models (design 02 §3, 03 §10): every CHECK / unique through IntegrityError, the FK ``on_delete``
rules, the derived ``pending`` flag, ordering, ``__str__`` and the indexes/constraints as PostgreSQL stores
them."""

from datetime import datetime, timedelta
from typing import Any

import pytest
from django.apps import apps
from django.db import IntegrityError, connection, transaction
from django.db.models import ProtectedError

from glowderma.core import enums
from glowderma.core.labels import stamp
from glowderma.core.models import OutboxMessage
from glowderma.core.timeutils import TASHKENT
from glowderma.delivery.models import PENDING_MESSAGE_STATUSES, Shipment, ShipmentEvent
from glowderma.delivery.tests.factories import (
    OrderRowFactory,
    ShipmentEventFactory,
    ShipmentFactory,
    create_message_for,
)

pytestmark = pytest.mark.django_db

T0 = datetime(2026, 10, 23, 14, 2, tzinfo=TASHKENT)


def violates(constraint: str, create: Any, **kwargs: Any) -> None:
    with pytest.raises(IntegrityError, match=constraint), transaction.atomic():
        create(**kwargs)


def index_definitions(table: str) -> dict[str, str]:
    with connection.cursor() as cursor:
        cursor.execute("SELECT indexname, indexdef FROM pg_indexes WHERE tablename = %s", [table])
        return dict(cursor.fetchall())


def constraint_definitions(table: str, types: str = "cu") -> dict[str, str]:
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT conname, pg_get_constraintdef(oid) FROM pg_constraint"
            " WHERE conrelid = %s::regclass AND contype = ANY(%s)",
            [table, list(types)],
        )
        return dict(cursor.fetchall())


# --- Shipment ----------------------------------------------------------------------------------------------


def test_shipment_factory_defaults_str_and_traits() -> None:
    shipment = ShipmentFactory.create(order__created_at=T0)
    assert shipment.state == Shipment.State.NOT_CREATED and shipment.created_at == T0
    assert shipment.tracking_number == "" and shipment.tariff is None and shipment.via == ""
    assert shipment.attempts == 0 and shipment.create_requests == 0 and shipment.create_message is None
    assert shipment.order.fulfilment == enums.FulfilmentMethod.DELIVERY
    assert str(shipment) == f"Buyurtma №{shipment.order_id} jo‘natmasi: Yaratilmagan"
    assert ShipmentFactory.create(not_created=True).state == "not_created"
    failed = ShipmentFactory.create(failed=True)
    assert failed.attempts == 1 and failed.last_attempt_at is not None and failed.last_error_code
    assert failed.order.status == enums.OrderStatus.PROCESSING
    created = ShipmentFactory.create(created=True)
    assert created.tracking_number.isdigit() and len(created.tracking_number) == 8
    assert created.tariff == created.order.delivery_fee == 25_000 and created.via == "polling"
    assert created.bts_created_at == created.last_status_at == created.last_attempt_at
    assert created.bts_status == "100" and created.cancelled_at is None
    assert str(created) == f"Buyurtma №{created.order_id} jo‘natmasi: Yaratilgan {created.tracking_number}"
    cancelled = ShipmentFactory.create(cancelled=True)
    assert cancelled.cancelled_at is not None and cancelled.bts_created_at is not None
    assert cancelled.order.status == enums.OrderStatus.CANCELLED and cancelled.order.paid_at is not None
    assert str(cancelled).endswith(f"Bekor qilingan {cancelled.tracking_number}")


def test_shipment_one_per_order_and_protect() -> None:
    shipment = ShipmentFactory.create()
    assert shipment.order.shipment == shipment
    # the refused duplicate leaves an unsaved instance in the order's reverse cache; nothing reaches the table
    violates("delivery_shipment_order_id_key", ShipmentFactory.create, order=shipment.order)
    assert Shipment.objects.filter(order=shipment.order).count() == 1
    with pytest.raises(ProtectedError):
        shipment.order.delete()
    assert Shipment.objects.filter(pk=shipment.pk).exists()


def test_shipment_tariff_nonneg_and_counters() -> None:
    violates("shipment_tariff_nonneg", ShipmentFactory.create, tariff=-1)
    assert ShipmentFactory.create(tariff=0).tariff == 0  # a free parcel is a valid quote
    assert ShipmentFactory.create(created=True, tariff=0).tariff == 0
    violates("delivery_shipment_attempts_check", ShipmentFactory.create, attempts=-1)
    violates("delivery_shipment_create_requests_check", ShipmentFactory.create, create_requests=-1)


def test_shipment_state_fields_not_created() -> None:
    violates("shipment_state_fields", ShipmentFactory.create, tracking_number="48213907")
    violates("shipment_state_fields", ShipmentFactory.create, bts_created_at=T0)
    violates("shipment_state_fields", ShipmentFactory.create, cancelled_at=T0)
    violates("shipment_state_fields", ShipmentFactory.create, state="bogus")
    # a not-created row may already count enqueues and carry a quote
    row = ShipmentFactory.create(create_requests=2, tariff=25_000, bts_status="", last_error="")
    assert row.state == "not_created"


def test_shipment_state_fields_failed() -> None:
    violates("shipment_state_fields", ShipmentFactory.create, failed=True, attempts=0)
    violates("shipment_state_fields", ShipmentFactory.create, failed=True, last_attempt_at=None)
    violates("shipment_state_fields", ShipmentFactory.create, failed=True, tracking_number="48213907")
    violates("shipment_state_fields", ShipmentFactory.create, failed=True, bts_created_at=T0)
    violates("shipment_state_fields", ShipmentFactory.create, failed=True, cancelled_at=T0)
    assert ShipmentFactory.create(failed=True, attempts=3, create_requests=3).attempts == 3


def test_shipment_state_fields_created() -> None:
    violates("shipment_state_fields", ShipmentFactory.create, created=True, tracking_number="")
    violates("shipment_state_fields", ShipmentFactory.create, created=True, bts_created_at=None)
    violates("shipment_state_fields", ShipmentFactory.create, created=True, tariff=None)  # D-2 fallback
    violates("shipment_state_fields", ShipmentFactory.create, created=True, cancelled_at=T0)
    # the counters are not part of the created clause (a parcel adopted by lookup has attempts 0)
    adopted = ShipmentFactory.create(created=True, attempts=0, last_attempt_at=None)
    assert adopted.state == "created"


def test_shipment_state_fields_cancelled_only_after_created() -> None:
    violates("shipment_state_fields", ShipmentFactory.create, cancelled=True, cancelled_at=None)
    violates("shipment_state_fields", ShipmentFactory.create, cancelled=True, bts_created_at=None)
    violates("shipment_state_fields", ShipmentFactory.create, cancelled=True, tracking_number="")
    violates("shipment_state_fields", ShipmentFactory.create, cancelled=True, tariff=None)
    # a parcel that never existed cannot be cancelled: not_created/failed rows refuse cancelled_at above,
    # and the cancelled state itself demands the created columns
    violates(
        "shipment_state_fields",
        ShipmentFactory.create,
        state=Shipment.State.CANCELLED,
        cancelled_at=T0,
    )


def test_shipment_pending_is_derived_from_the_create_message() -> None:
    assert PENDING_MESSAGE_STATUSES == ("pending", "failed", "in_flight")
    no_message = ShipmentFactory.create()
    by_status: dict[str, Shipment] = {}
    for status in ("pending", "failed", "in_flight", "done", "dead"):
        shipment = ShipmentFactory.create(failed=True)
        create_message_for(shipment, status=status)
        by_status[status] = shipment
    flags = {row.pk: row.pending for row in Shipment.objects.with_pending()}  # type: ignore[attr-defined]
    assert flags[no_message.pk] is False
    assert {status: flags[s.pk] for status, s in by_status.items()} == {
        "pending": True,
        "failed": True,
        "in_flight": True,
        "done": False,
        "dead": False,
    }
    assert no_message.is_pending is False
    assert by_status["in_flight"].is_pending is True and by_status["done"].is_pending is False
    pending_ids = set(Shipment.objects.filter(Shipment.pending_q()).values_list("pk", flat=True))
    assert pending_ids == {by_status[s].pk for s in ("pending", "failed", "in_flight")}
    # the same flag from an order (the orders-list annotation walks shipment__create_message)
    order_model = apps.get_model("commerce", "Order")
    annotated = dict(
        order_model.objects.annotate(shipment_pending=Shipment.pending_expression("shipment__")).values_list(
            "pk", "shipment_pending"
        )
    )
    assert annotated[by_status["pending"].order_id] is True
    assert annotated[by_status["dead"].order_id] is False and annotated[no_message.order_id] is False
    assert set(order_model.objects.filter(Shipment.pending_q("shipment__")).values_list("pk", flat=True)) == {
        by_status[s].order_id for s in ("pending", "failed", "in_flight")
    }


def test_shipment_create_message_set_null_on_outbox_purge() -> None:
    shipment = ShipmentFactory.create()
    message = create_message_for(shipment)
    # compare ids, not instances: an instance `==` would let mypy narrow create_message to non-Optional
    # for the rest of the function and silently skip the checks after the purge below
    assert shipment.create_requests == 1 and shipment.create_message_id == message.pk
    assert message.dedupe_key == f"bts.create_shipment:{shipment.order_id}:1"
    assert message.payload == {
        "order_id": shipment.order_id,
        "manual": False,
        "request_no": 1,
        "actor_id": None,
    }
    second = create_message_for(shipment, status="in_flight", manual=True)
    assert second.dedupe_key.endswith(":2") and shipment.create_requests == 2
    assert OutboxMessage.objects.filter(pk=message.pk).exists()  # the older message is simply superseded
    OutboxMessage.objects.filter(pk=second.pk).delete()  # retention purge of the outbox
    shipment = Shipment.objects.get(pk=shipment.pk)  # a fresh load, so no stale narrowing survives
    assert shipment.create_message_id is None
    assert shipment.create_message is None
    assert shipment.is_pending is False
    assert Shipment.objects.with_pending().get(pk=shipment.pk).pending is False  # type: ignore[attr-defined]


def test_shipment_indexes_and_constraints_in_pg_catalog() -> None:
    indexes = index_definitions("delivery_shipment")
    assert "(state)" in indexes["shipment_state_idx"]
    assert (
        "(tracking_number) WHERE (NOT ((tracking_number)::text = ''::text))"
        in (indexes["shipment_tracking_idx"])
    )
    assert indexes["delivery_shipment_order_id_key"].startswith("CREATE UNIQUE INDEX")
    constraints = constraint_definitions("delivery_shipment")
    assert constraints["delivery_shipment_order_id_key"] == "UNIQUE (order_id)"
    assert constraints["shipment_tariff_nonneg"] == "CHECK (((tariff IS NULL) OR (tariff >= 0)))"
    state_fields = constraints["shipment_state_fields"]
    for literal in ("'not_created'", "'failed'", "'created'", "'cancelled'"):
        assert literal in state_fields
    assert "attempts >= 1" in state_fields and "tariff IS NOT NULL" in state_fields
    assert "cancelled_at IS NOT NULL" in state_fields
    assert {"delivery_shipment_attempts_check", "delivery_shipment_create_requests_check"} <= set(constraints)


# --- ShipmentEvent -----------------------------------------------------------------------------------------


def test_shipment_event_factory_defaults_str_ordering_and_protect() -> None:
    event = ShipmentEventFactory.create()
    shipment = event.shipment
    assert event.kind == ShipmentEvent.Kind.CREATED and event.bts_status == "100" and event.via == "polling"
    assert event.manual is False and event.error_code == "" and event.at == shipment.bts_created_at
    assert event.payload == {"tracking_number": shipment.tracking_number, "status": "100"}
    assert str(event) == f"Jo‘natma #{shipment.pk}: Yaratildi 100 · {stamp(event.at)}"
    assert ShipmentEvent.Kind.values == [
        "created",
        "create_failed",
        "status_received",
        "cancelled",
        "cancel_refused",
    ]
    later = ShipmentEventFactory.create(
        shipment=shipment,
        kind=ShipmentEvent.Kind.STATUS_RECEIVED,
        bts_status="200",
        via=enums.ShipmentVia.WEBHOOK,
        at=event.at + timedelta(hours=2),
        payload={"status": 200},
    )
    tie = ShipmentEventFactory.create(shipment=shipment, kind=ShipmentEvent.Kind.CANCEL_REFUSED, at=later.at)
    earlier = ShipmentEventFactory.create(
        shipment=shipment,
        kind=ShipmentEvent.Kind.CREATE_FAILED,
        error_code="bts_rejected",
        at=event.at - timedelta(minutes=5),
    )
    assert list(shipment.events.all()) == [earlier, event, later, tie]
    other = ShipmentEventFactory.create()
    assert list(ShipmentEvent.objects.all()) == sorted(
        [earlier, event, later, tie, other], key=lambda e: (e.shipment_id, e.at, e.pk)
    )
    with pytest.raises(ProtectedError):
        shipment.delete()  # PROTECT from the append-only side
    assert ShipmentEvent.objects.filter(shipment=shipment).count() == 4
    with pytest.raises(ProtectedError):
        shipment.order.delete()


def test_shipment_event_index_in_pg_catalog() -> None:
    indexes = index_definitions("delivery_shipmentevent")
    assert "(shipment_id, at, id)" in indexes["shipmentevent_shipment_at_idx"]
    assert constraint_definitions("delivery_shipmentevent", "c") == {}


def test_order_row_factory_covers_what_the_module_needs() -> None:
    order = OrderRowFactory.create(created_at=T0)
    assert order.total == 210_000 and order.status == enums.OrderStatus.AWAITING_PAYMENT
    assert order.reservation_expires_at == T0 + timedelta(minutes=15)
    paid = OrderRowFactory.create(paid=True, payme=True)
    assert paid.status == "processing" and paid.paid_at is not None and paid.payment_provider == "payme"
    cancelled = OrderRowFactory.create(cancelled=True)
    assert cancelled.cancelled_by is not None and cancelled.cancellation_reason == "admin"
