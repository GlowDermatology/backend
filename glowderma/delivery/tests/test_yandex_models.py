"""Yandex Delivery data model (plan §3.1): constraints, the settings singleton, the calendar selector, the
exception queue services and the quote purge."""

from collections.abc import Callable
from datetime import date, time, timedelta
from decimal import Decimal
from typing import Any

import pytest
from django.db import IntegrityError, transaction

from glowderma.accounts.tests.factories import StaffUserFactory
from glowderma.core import enums, timeutils
from glowderma.core.models import AuditEntry
from glowderma.delivery import selectors, services
from glowderma.delivery.apps import ensure_settings
from glowderma.delivery.models import (
    CourierClaim,
    DeliveryIssue,
    DeliveryQuote,
    DeliverySettings,
    Shipment,
    ShipmentEvent,
    WarehouseClosure,
    WarehouseHours,
)
from glowderma.delivery.tests.factories import (
    CourierClaimFactory,
    DeliveryIssueFactory,
    DeliveryQuoteFactory,
    OrderRowFactory,
    ShipmentFactory,
    yandex_shipment,
)

pytestmark = pytest.mark.django_db

K = enums.DeliveryIssueKind


def violates(name: str, build: Callable[..., Any], **kwargs: Any) -> None:
    with pytest.raises(IntegrityError, match=name), transaction.atomic():
        build(**kwargs)


# --- settings singleton, hours, closures -------------------------------------------------------------------


def test_the_settings_singleton_exists_with_the_planned_defaults() -> None:
    row = DeliverySettings.objects.get(pk=1)  # delivery.0007 / post_migrate
    assert DeliverySettings.load() == row
    assert (row.prep_minutes, row.dispatch_cutoff_minutes, row.prep_deadline_minutes) == (30, 90, 120)
    assert row.returns_accepted and not row.dispatch_suspended and row.suspended_until is None
    assert not row.standard_enabled and not row.express_enabled
    assert (row.standard_taxi_class, row.express_taxi_class) == ("courier", "express")
    assert (row.standard_eta_min_minutes, row.standard_eta_max_minutes) == (60, 180)
    assert (row.express_eta_min_minutes, row.express_eta_max_minutes) == (30, 90)
    assert row.default_item_weight_g == 300
    assert (row.markup_percent, row.estimate_vat_percent) == (Decimal("10.00"), Decimal("0.00"))
    assert (row.max_auto_increase, row.max_loss_per_shipment) == (10_000, 15_000)
    assert str(row) == "Yetkazish sozlamalari"


def test_post_migrate_re_ensures_the_singleton() -> None:
    DeliverySettings.objects.all().delete()
    from django.apps import apps

    ensure_settings(apps.get_app_config("delivery"), using="default")
    assert DeliverySettings.objects.filter(pk=1).exists()


@pytest.mark.parametrize(
    ("fields", "constraint"),
    [
        ({"id": 2}, "deliverysettings_singleton"),
        ({"warehouse_latitude": Decimal("41.3")}, "deliverysettings_latlng_pair"),
        (
            {"warehouse_latitude": Decimal("69.24"), "warehouse_longitude": Decimal("41.31")},
            "deliverysettings_latlng_uzbekistan",
        ),  # swapped pin
        ({"contact_phone": "998901234567"}, "deliverysettings_contact_phone_e164"),
        ({"prep_minutes": 481}, "deliverysettings_minutes_range"),
        ({"prep_deadline_minutes": 10}, "deliverysettings_minutes_range"),
        ({"standard_eta_min_minutes": 200}, "deliverysettings_eta_range"),
        ({"express_eta_min_minutes": 0}, "deliverysettings_eta_range"),
        ({"standard_taxi_class": ""}, "deliverysettings_taxi_class_required"),
        ({"default_item_weight_g": 0}, "deliverysettings_item_weight_range"),
        ({"markup_percent": Decimal("100.01")}, "deliverysettings_policy_range"),
        ({"max_loss_per_shipment": -1}, "deliverysettings_policy_range"),
    ],
)
def test_settings_checks(fields: dict[str, Any], constraint: str) -> None:
    def save() -> None:
        if "id" in fields:
            DeliverySettings.objects.create(**fields)
        else:
            DeliverySettings.objects.filter(pk=1).update(**fields)

    violates(constraint, save)


def test_a_valid_warehouse_saves() -> None:
    DeliverySettings.objects.filter(pk=1).update(
        warehouse_latitude=Decimal("41.311081"),
        warehouse_longitude=Decimal("69.240562"),
        contact_phone="+998901234567",
    )


def test_hours_and_closures_checks() -> None:
    WarehouseHours.objects.create(weekday=0, opens_at=time(9), closes_at=time(20))
    violates(
        "warehousehours_weekday_key",
        WarehouseHours.objects.create,
        weekday=0,
        opens_at=time(9),
        closes_at=time(10),
    )
    violates(
        "warehousehours_weekday",
        WarehouseHours.objects.create,
        weekday=7,
        opens_at=time(9),
        closes_at=time(10),
    )
    violates(
        "warehousehours_order", WarehouseHours.objects.create, weekday=1, opens_at=time(10), closes_at=time(9)
    )
    WarehouseClosure.objects.create(date=date(2026, 12, 31))
    WarehouseClosure.objects.create(date=date(2027, 1, 1), opens_at=time(10), closes_at=time(14))
    violates(
        "warehouseclosure_times", WarehouseClosure.objects.create, date=date(2027, 1, 2), opens_at=time(10)
    )
    violates(
        "warehouseclosure_times",
        WarehouseClosure.objects.create,
        date=date(2027, 1, 3),
        opens_at=time(14),
        closes_at=time(10),
    )
    violates("warehouseclosure_date_key", WarehouseClosure.objects.create, date=date(2026, 12, 31))


def test_warehouse_calendar_selector_reads_the_rows() -> None:
    today = date(2026, 11, 16)
    calendar = selectors.warehouse_calendar(today)
    assert not calendar.configured and calendar.hours == {} and calendar.closures == {}
    DeliverySettings.objects.filter(pk=1).update(
        warehouse_latitude=Decimal("41.311081"),
        warehouse_longitude=Decimal("69.240562"),
        warehouse_address="Toshkent, Yunusobod 4",
        contact_name="Ombor",
        contact_phone="+998901234567",
        prep_minutes=20,
        dispatch_suspended=True,
    )
    WarehouseHours.objects.create(weekday=0, opens_at=time(9), closes_at=time(20))
    WarehouseClosure.objects.create(date=today, opens_at=time(10), closes_at=time(12))
    WarehouseClosure.objects.create(date=today + timedelta(days=1))
    WarehouseClosure.objects.create(date=today + timedelta(days=60))  # beyond the horizon: not read
    calendar = selectors.warehouse_calendar(today)
    assert calendar.configured and calendar.prep_minutes == 20 and calendar.dispatch_suspended
    assert calendar.hours == {0: (time(9), time(20))}
    assert calendar.closures == {today: (time(10), time(12)), today + timedelta(days=1): None}
    standard = selectors.method_settings("standard")
    assert (standard.enabled, standard.taxi_class, standard.eta_min_minutes) == (False, "courier", 60)
    with pytest.raises(ValueError):
        selectors.method_settings("cargo")


def test_reads_never_create_the_settings_row() -> None:
    DeliverySettings.objects.all().delete()
    calendar = selectors.warehouse_calendar()
    assert not calendar.configured and calendar.prep_minutes == 30
    assert selectors.method_settings("express").taxi_class == "express"
    assert not DeliverySettings.objects.exists()


# --- shipment -----------------------------------------------------------------------------------------------


def test_existing_and_new_rows_are_bts_by_default() -> None:
    shipment = ShipmentFactory.create(created=True)
    assert (shipment.provider, shipment.method, shipment.stage) == ("bts", "", "")
    assert shipment.carrier_cost_total == 0 and shipment.claim is None and shipment.is_bts
    assert list(Shipment.objects.bts()) == [shipment]


@pytest.mark.parametrize(
    ("state", "stage"),
    [
        ("not_created", ""),
        ("not_created", "awaiting_dispatch"),
        ("failed", "failed"),
        ("created", "in_transit"),
        ("cancelled", "cancelled"),
    ],
)
def test_a_yandex_shipment_in_every_state(state: str, stage: str) -> None:
    shipment = yandex_shipment(state=state, stage=stage)
    assert shipment.state == state and shipment.stage == stage and not shipment.is_bts
    assert shipment.tracking_number == "" and shipment.bts_created_at is None
    assert (shipment.claim is not None) == (state in ("created", "cancelled"))
    assert not Shipment.objects.bts().filter(pk=shipment.pk).exists()


def test_shipment_provider_checks() -> None:
    order = OrderRowFactory.create(paid=True)
    violates("shipment_provider_fields", ShipmentFactory.create, order=order, method="standard")
    violates("shipment_provider_fields", ShipmentFactory.create, order=order, stage="awaiting_dispatch")
    violates("shipment_provider_fields", ShipmentFactory.create, order=order, provider="yandex")
    # a Yandex row never carries BTS fields: nothing a BTS webhook or poll matches on
    violates(
        "shipment_state_fields",
        ShipmentFactory.create,
        order=order,
        yandex=True,
        tracking_number="48213907",
    )
    violates(
        "shipment_state_fields",
        ShipmentFactory.create,
        order=order,
        yandex=True,
        state="created",
        tariff=20_000,
    )  # created needs the accepted claim
    violates("shipment_yandex_amounts", ShipmentFactory.create, order=order, carrier_cost_total=-1)
    violates("shipment_yandex_amounts", ShipmentFactory.create, order=order, yandex=True, parcel_weight_g=0)
    now = timeutils.now()
    violates(
        "shipment_promised_window",
        ShipmentFactory.create,
        order=order,
        yandex=True,
        promised_from=now,
        promised_to=now - timedelta(minutes=1),
    )
    violates("shipment_promised_window", ShipmentFactory.create, order=order, yandex=True, promised_from=now)


def test_the_bts_state_branch_is_unchanged() -> None:
    order = OrderRowFactory.create(paid=True)
    violates("shipment_state_fields", ShipmentFactory.create, order=order, state="failed")  # no attempt
    violates("shipment_state_fields", ShipmentFactory.create, order=order, state="created", tariff=1)


# --- courier claims ----------------------------------------------------------------------------------------


def test_one_open_claim_per_shipment_and_numbering() -> None:
    first = CourierClaimFactory.create()
    assert first.number == 1 and first.is_open and str(first).endswith("1: new")
    violates("courierclaim_one_open_per_shipment", CourierClaimFactory.create, shipment=first.shipment)
    CourierClaim.objects.filter(pk=first.pk).update(ended_at=timeutils.now())
    second = CourierClaimFactory.create(shipment=first.shipment)
    assert second.number == 2 and second.request_id != first.request_id
    violates(
        "courierclaim_number_unique",
        CourierClaimFactory.create,
        shipment=first.shipment,
        number=2,
        ended_at=timeutils.now(),
    )
    assert list(first.shipment.claims.all()) == [first, second]


def test_claim_id_is_unique_only_when_known() -> None:
    a = CourierClaimFactory.create(claim_id="")
    b = CourierClaimFactory.create(claim_id="")
    assert a.claim_id == b.claim_id == ""
    known = CourierClaimFactory.create(claim_id="7b2c")
    violates("courierclaim_claim_id_unique", CourierClaimFactory.create, claim_id=known.claim_id)
    violates("courierclaim_request_id_key", CourierClaimFactory.create, request_id=known.request_id)


@pytest.mark.parametrize(
    ("fields", "constraint"),
    [
        ({"number": 0}, "courierclaim_number_positive"),
        ({"offer_price": -1}, "courierclaim_amounts_nonneg"),
        ({"final_price": -1}, "courierclaim_amounts_nonneg"),
        ({"accepted_at": "now"}, "courierclaim_accepted_after_sent"),
        ({"settled_at": "now"}, "courierclaim_settled_after_end"),
    ],
)
def test_claim_checks(fields: dict[str, Any], constraint: str) -> None:
    values = {k: timeutils.now() if v == "now" else v for k, v in fields.items()}
    violates(constraint, CourierClaimFactory.create, **values)


def test_shipment_event_carrier_status_and_new_kinds() -> None:
    shipment = yandex_shipment(state="created", stage="in_transit")
    event = ShipmentEvent.objects.create(
        shipment=shipment,
        kind=ShipmentEvent.Kind.CLAIM_STATUS,
        carrier_status="pickuped",
        payload={"version": 3},
    )
    assert event.bts_status == "" and str(event).startswith(
        f"Jo‘natma #{shipment.pk}: Yandex holati keldi pickuped"
    )
    bts_event = ShipmentEvent.objects.create(shipment=ShipmentFactory.create(created=True), kind="created")
    assert bts_event.carrier_status == ""


# --- quotes ----------------------------------------------------------------------------------------------


def test_quote_checks() -> None:
    quote = DeliveryQuoteFactory.create()
    assert quote.pk is not None and quote.used_at is None and quote.scheduled is False
    violates("deliveryquote_amounts", DeliveryQuoteFactory.create, customer_fee=-1)
    violates("deliveryquote_amounts", DeliveryQuoteFactory.create, weight_g=0)
    violates("deliveryquote_latlng_pair", DeliveryQuoteFactory.create, longitude=None)
    violates("deliveryquote_provider_fields", DeliveryQuoteFactory.create, latitude=None, longitude=None)
    violates("deliveryquote_provider_fields", DeliveryQuoteFactory.create, method="")
    violates(
        "deliveryquote_provider_fields", DeliveryQuoteFactory.create, provider="bts"
    )  # BTS has no method
    DeliveryQuoteFactory.create(provider="bts", method="", latitude=None, longitude=None, region_code="10")
    now = timeutils.now()
    violates("deliveryquote_eta_window", DeliveryQuoteFactory.create, eta_from=now, eta_to=now - timedelta(1))
    violates("deliveryquote_expiry", DeliveryQuoteFactory.create, created_at=now, expires_at=now)
    order = OrderRowFactory.create()
    violates("deliveryquote_order_when_used", DeliveryQuoteFactory.create, order=order)
    DeliveryQuoteFactory.create(order=order, used_at=now)


def test_purge_quotes_keeps_used_and_unexpired_quotes() -> None:
    now = timeutils.now()
    old = now - timedelta(days=2)
    expired = DeliveryQuoteFactory.create(created_at=old)
    used = DeliveryQuoteFactory.create(created_at=old, used_at=old + timedelta(minutes=5))
    fresh = DeliveryQuoteFactory.create()
    shipment = yandex_shipment(quote=used)
    assert services.purge_quotes(now - timedelta(days=1)) == 1
    assert set(DeliveryQuote.objects.values_list("pk", flat=True)) == {used.pk, fresh.pk}
    assert not DeliveryQuote.objects.filter(pk=expired.pk).exists()
    shipment.refresh_from_db()
    assert shipment.quote_id == used.pk


# --- exception queue -------------------------------------------------------------------------------------


def test_open_issue_is_idempotent_and_refreshes_the_open_row() -> None:
    order = OrderRowFactory.create(paid=True)
    issue_id, created = services.open_issue(
        order.pk, K.DISPATCH_FAILED, code="offer_expired", urgent=False, details={"claim": 1}
    )
    assert created
    again, created = services.open_issue(
        order.pk, K.DISPATCH_FAILED, code="performer_not_found", urgent=True, details={"claim": 2}
    )
    assert (again, created) == (issue_id, False)
    row = DeliveryIssue.objects.get(pk=issue_id)
    assert (row.code, row.urgent, row.details, row.note) == ("performer_not_found", True, {"claim": 2}, "")
    other, created = services.open_issue(order.pk, K.DELIVERY_FAILED, code="", urgent=False, details={})
    assert created and other != issue_id
    assert selectors.issue_counts() == {"open": 2, "urgent": 1}
    assert [i.id for i in selectors.open_issues()] == [issue_id, other]  # urgent first
    assert [i.id for i in selectors.open_issues(kind=K.DELIVERY_FAILED)] == [other]
    assert selectors.has_open_issue(order.pk, K.DISPATCH_FAILED)
    with pytest.raises(ValueError):
        services.open_issue(order.pk, "nonsense", code="", urgent=False, details={})
    with pytest.raises(ValueError):
        services.open_issue(order.pk, K.INCIDENT, code="", urgent=False, details={}, note="x" * 1001)


def test_resolve_issue_is_audited_and_a_new_issue_can_open_afterwards() -> None:
    staff = StaffUserFactory.create()
    issue = DeliveryIssueFactory.create(note="Kuryer kelmadi")
    assert services.resolve_issue(issue.pk, actor_id=staff.pk, note="  Qayta chaqirildi ")
    issue.refresh_from_db()
    assert (
        issue.state == "resolved"
        and issue.resolved_by == staff
        and issue.resolution_note == "Qayta chaqirildi"
    )
    assert issue.resolved_at is not None
    entry = AuditEntry.objects.get(action="delivery.issue_resolved")
    assert entry.entity_id == str(issue.pk) and entry.after is not None
    assert entry.after["kind"] == "dispatch_failed"
    assert not services.resolve_issue(issue.pk, actor_id=staff.pk, note="")  # already resolved: no-op
    assert AuditEntry.objects.filter(action="delivery.issue_resolved").count() == 1
    new_id, created = services.open_issue(issue.order_id, issue.kind, code="x", urgent=False, details={})
    assert created and new_id != issue.pk
    with pytest.raises(services.IssueNotFound):
        services.resolve_issue(10**9, actor_id=staff.pk, note="")


def test_resolve_kind_closes_without_an_actor() -> None:
    issue = DeliveryIssueFactory.create(kind=K.RETURN_PENDING)
    assert services.resolve_kind(issue.order_id, K.RETURN_PENDING, note="returned")
    issue.refresh_from_db()
    assert issue.state == "resolved" and issue.resolved_by is None and issue.resolution_note == "returned"
    assert not services.resolve_kind(issue.order_id, K.RETURN_PENDING)
    assert not AuditEntry.objects.filter(action="delivery.issue_resolved").exists()
    assert selectors.issue_counts() == {"open": 0, "urgent": 0}


def test_issue_constraints() -> None:
    issue = DeliveryIssueFactory.create()
    violates("deliveryissue_one_open_per_kind", DeliveryIssueFactory.create, order=issue.order)
    violates("deliveryissue_resolution_fields", DeliveryIssueFactory.create, state="resolved")
    violates(
        "deliveryissue_resolution_fields",
        DeliveryIssueFactory.create,
        resolved_at=timeutils.now(),
    )
    DeliveryIssueFactory.create(order=issue.order, state="resolved", resolved_at=timeutils.now())
    assert str(issue) == f"Buyurtma №{issue.order_id}: Kuryer chaqirilmadi (Ochiq)"


# --- payment of a Yandex order ---------------------------------------------------------------------------


def test_mark_awaiting_dispatch_sets_the_stage_and_the_promised_window() -> None:
    shipment = yandex_shipment()
    paid_at = timeutils.now()
    assert services.mark_awaiting_dispatch(shipment.order_id, at=paid_at)
    shipment.refresh_from_db()
    assert shipment.stage == "awaiting_dispatch"
    assert shipment.promised_from is None and shipment.promised_to is None  # no hours configured yet
    assert not services.mark_awaiting_dispatch(shipment.order_id, at=paid_at)  # replay
    for weekday in range(7):
        WarehouseHours.objects.create(weekday=weekday, opens_at=time(0), closes_at=time(23, 59))
    other = yandex_shipment(method="express")
    monday_noon = timeutils.day_bounds(date(2026, 11, 16))[0] + timedelta(hours=12)
    assert services.mark_awaiting_dispatch(other.order_id, at=monday_noon)
    other.refresh_from_db()
    assert other.promised_from == monday_noon + timedelta(minutes=30 + 30)
    assert other.promised_to == monday_noon + timedelta(minutes=30 + 90)
    with pytest.raises(services.DeliveryError):
        services.mark_awaiting_dispatch(ShipmentFactory.create().order_id, at=paid_at)


def test_the_bts_branch_of_shipment_state_fields_is_the_original_condition() -> None:
    """Decision 3: BTS behaviour byte-for-byte — the BTS branch equals the condition delivery.0001 created."""
    from importlib import import_module

    from glowderma.delivery.models import _BTS_STATE_FIELDS

    initial = import_module("glowderma.delivery.migrations.0001_initial").Migration
    (original,) = [
        op.constraint.condition
        for op in initial.operations
        if op.__class__.__name__ == "AddConstraint" and op.constraint.name == "shipment_state_fields"
    ]
    assert original == _BTS_STATE_FIELDS
