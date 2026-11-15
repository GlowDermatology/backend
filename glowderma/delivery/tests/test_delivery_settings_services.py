"""The audited writes behind the delivery settings page and the admin API (Yandex Delivery plan §3.9-§3.10):
``update_settings`` (validation, the live-mode method rule, PII-safe diff), ``set_suspension``, the calendar
rows, ``check_yandex_tariffs``, ``open_incident`` and the quote purge task."""

from collections.abc import Iterator
from datetime import date, time, timedelta
from decimal import Decimal
from typing import Any

import pytest
from django.core.exceptions import ValidationError
from django.test import override_settings

from glowderma.accounts.tests.factories import StaffUserFactory
from glowderma.core import timeutils
from glowderma.core.models import AuditEntry, OutboxMessage
from glowderma.delivery import services, tasks
from glowderma.delivery.models import (
    DeliveryIssue,
    DeliveryQuote,
    DeliverySettings,
    WarehouseClosure,
    WarehouseHours,
)
from glowderma.delivery.tests.factories import DeliveryQuoteFactory, OrderRowFactory
from glowderma.integrations import selectors as integration_selectors
from glowderma.integrations import services as integration_services
from glowderma.integrations.yandex_delivery import fake as fake_yandex
from glowderma.integrations.yandex_delivery.ports import Tariff, YandexRejected, YandexUnavailable

pytestmark = pytest.mark.django_db

PIN = {"warehouse_latitude": Decimal("41.311081"), "warehouse_longitude": Decimal("69.240562")}
WAREHOUSE = {**PIN, "warehouse_address": "Toshkent, Yunusobod, 4-mavze, 12", "warehouse_name": "Ombor"}


@pytest.fixture(autouse=True)
def fake_reset() -> Iterator[None]:
    fake_yandex.reset()
    yield
    fake_yandex.reset()


def _doc(value: Any) -> dict[str, Any]:
    assert isinstance(value, dict)
    return value


def _messages(error: ValidationError) -> dict[str, list[str]]:
    return {field: [str(message) for message in messages] for field, messages in error.message_dict.items()}


# --- update_settings ----------------------------------------------------------------------------------------


def test_update_settings_writes_the_changes_and_audits_only_the_diff() -> None:
    staff = StaffUserFactory.create()
    row = services.update_settings(
        {**WAREHOUSE, "contact_name": "Malika", "contact_phone": "+998 90 123-45-67", "prep_minutes": 45},
        actor=staff,
    )
    assert row.contact_phone == "+998901234567" and row.prep_minutes == 45 and row.updated_by == staff
    stored = DeliverySettings.objects.get(pk=1)
    assert stored.warehouse_latitude == PIN["warehouse_latitude"] and stored.updated_by_id == staff.pk
    entry = AuditEntry.objects.get(action=services.AUDIT_SETTINGS_UPDATED)
    assert entry.entity_type == "delivery_settings" and entry.entity_id == "1" and entry.actor_id == staff.pk
    # the address and the contact are audited as "changed", never their values
    assert _doc(entry.after)["warehouse_address"] == {"changed": True}
    assert _doc(entry.after)["contact_phone"] == {"changed": True}
    assert _doc(entry.after)["contact_name"] == {"changed": True}
    assert _doc(entry.after)["prep_minutes"] == {"from": 30, "to": 45}
    assert _doc(entry.after)["warehouse_latitude"] == {"from": None, "to": "41.311081"}
    assert "998901234567" not in str(entry.after) and "Yunusobod" not in str(entry.after)
    # the same values again: nothing written, no second audit row
    services.update_settings({"prep_minutes": 45}, actor=staff)
    assert AuditEntry.objects.filter(action=services.AUDIT_SETTINGS_UPDATED).count() == 1


@pytest.mark.parametrize(
    ("changes", "field"),
    [
        ({"warehouse_latitude": Decimal("41.3")}, "warehouse_longitude"),
        (
            {"warehouse_latitude": Decimal("55.75"), "warehouse_longitude": Decimal("37.61")},
            "warehouse_latitude",
        ),
        (
            {"warehouse_latitude": Decimal("41.3"), "warehouse_longitude": Decimal("37.61")},
            "warehouse_longitude",
        ),
        ({"contact_phone": "+7 999 123 45 67"}, "contact_phone"),
        ({"prep_minutes": 481}, "prep_minutes"),
        ({"prep_deadline_minutes": 10}, "prep_deadline_minutes"),
        ({"markup_percent": Decimal("100.01")}, "markup_percent"),
        ({"default_item_weight_g": 0}, "default_item_weight_g"),
        ({"express_eta_min_minutes": 100, "express_eta_max_minutes": 90}, "express_eta_max_minutes"),
        ({"standard_taxi_class": "cargo"}, "standard_taxi_class"),
    ],
)
def test_update_settings_refuses_invalid_values(changes: dict[str, Any], field: str) -> None:
    with pytest.raises(ValidationError) as caught:
        services.update_settings(changes, actor=StaffUserFactory.create())
    assert field in caught.value.message_dict
    assert not AuditEntry.objects.filter(action=services.AUDIT_SETTINGS_UPDATED).exists()
    assert DeliverySettings.objects.get(pk=1).updated_by_id is None


def test_update_settings_refuses_unknown_columns() -> None:
    with pytest.raises(ValueError, match="Not editable"):
        services.update_settings({"updated_at": timeutils.now()}, actor=None)


def test_a_method_may_be_enabled_freely_in_fake_mode() -> None:
    row = services.update_settings({"express_enabled": True, "standard_enabled": True}, actor=None)
    assert row.express_enabled and row.standard_enabled


@override_settings(YANDEX_DELIVERY_MODE="live")
def test_live_mode_enables_a_method_only_with_a_class_yandex_offers() -> None:
    with pytest.raises(ValidationError) as unchecked:
        services.update_settings({"express_enabled": True}, actor=None)
    assert "Yandex bilan tekshirish" in _messages(unchecked.value)["express_enabled"][0]
    # a check in fake mode does not count in live mode
    integration_services.update_provider_data(
        "yandex", {"tariffs": ["courier", "express"], "tariffs_mode": "fake"}
    )
    with pytest.raises(ValidationError):
        services.update_settings({"express_enabled": True}, actor=None)
    integration_services.update_provider_data("yandex", {"tariffs": ["courier"], "tariffs_mode": "live"})
    with pytest.raises(ValidationError) as missing:
        services.update_settings({"express_enabled": True}, actor=None)
    assert "express" in _messages(missing.value)["express_enabled"][0]
    row = services.update_settings({"standard_enabled": True}, actor=None)
    assert row.standard_enabled
    # an unrelated save of an enabled method is never refused by a later answer without its class
    integration_services.update_provider_data("yandex", {"tariffs": [], "tariffs_mode": "live"})
    assert services.update_settings({"prep_minutes": 40}, actor=None).prep_minutes == 40
    # moving the enabled method to a class Yandex does not offer is
    with pytest.raises(ValidationError):
        services.update_settings({"standard_taxi_class": "express"}, actor=None)


# --- suspension ---------------------------------------------------------------------------------------------


def test_set_suspension_audits_the_switch_and_resume_clears_until() -> None:
    staff = StaffUserFactory.create()
    until = timeutils.now() + timedelta(hours=2)
    current = services.set_suspension(True, until, " Inventarizatsiya ", actor=staff)
    assert current.suspended and current.until == until and current.note == "Inventarizatsiya"
    row = DeliverySettings.objects.get(pk=1)
    assert row.dispatch_suspended and row.suspended_until == until and row.updated_by_id == staff.pk
    entry = AuditEntry.objects.get(action=services.AUDIT_SUSPENSION_UPDATED)
    assert entry.before == {"suspended": False, "until": None, "note": ""}
    assert entry.after == {"suspended": True, "until": until.isoformat(), "note": "Inventarizatsiya"}
    # the same switch again: no second row
    services.set_suspension(True, until, "Inventarizatsiya", actor=staff)
    assert AuditEntry.objects.filter(action=services.AUDIT_SUSPENSION_UPDATED).count() == 1
    resumed = services.set_suspension(False, until, "", actor=staff)
    assert not resumed.suspended and resumed.until is None
    assert DeliverySettings.objects.get(pk=1).suspended_until is None
    assert AuditEntry.objects.filter(action=services.AUDIT_SUSPENSION_UPDATED).count() == 2


def test_set_suspension_refuses_a_past_until_and_reports_an_expired_one_as_off() -> None:
    with pytest.raises(ValueError, match="future"):
        services.set_suspension(True, timeutils.now() - timedelta(minutes=1), "", actor=None)
    until = timeutils.now() + timedelta(minutes=30)
    services.set_suspension(True, until, "", actor=None)
    row = DeliverySettings.objects.get(pk=1)
    assert services.suspension_of(row, until - timedelta(minutes=1)).suspended
    later = services.suspension_of(row, until + timedelta(minutes=1))
    assert not later.suspended and later.until is None
    services.set_suspension(True, None, "", actor=None)
    assert services.suspension_of(DeliverySettings.objects.get(pk=1)).suspended  # until lifted


# --- calendar -----------------------------------------------------------------------------------------------


def test_hours_and_closures_are_saved_and_deleted_through_audited_services() -> None:
    staff = StaffUserFactory.create()
    monday = services.save_hours(WarehouseHours(weekday=0, opens_at=time(9), closes_at=time(18)), actor=staff)
    monday.closes_at = time(19)
    services.save_hours(monday, actor=staff)
    saved = AuditEntry.objects.filter(action=services.AUDIT_HOURS_SAVED).order_by("id")
    assert [entry.before for entry in saved] == [
        None,
        {"weekday": 0, "opens_at": "09:00:00", "closes_at": "18:00:00"},
    ]
    with pytest.raises(ValidationError) as order:
        services.save_hours(WarehouseHours(weekday=1, opens_at=time(18), closes_at=time(9)), actor=staff)
    assert "closes_at" in order.value.message_dict
    with pytest.raises(ValidationError) as taken:
        services.save_hours(WarehouseHours(weekday=0, opens_at=time(10), closes_at=time(12)), actor=staff)
    assert "weekday" in taken.value.message_dict
    services.delete_hours(monday, actor=staff)
    assert not WarehouseHours.objects.exists()
    assert _doc(AuditEntry.objects.get(action=services.AUDIT_HOURS_DELETED).before)["weekday"] == 0

    holiday = services.save_closure(WarehouseClosure(date=date(2026, 12, 31), note=" Bayram "), actor=staff)
    assert holiday.created_by == staff and holiday.note == "Bayram"
    with pytest.raises(ValidationError):
        services.save_closure(WarehouseClosure(date=date(2027, 1, 2), opens_at=time(10)), actor=staff)
    short = services.save_closure(
        WarehouseClosure(date=date(2027, 1, 3), opens_at=time(10), closes_at=time(14)), actor=staff
    )
    assert AuditEntry.objects.filter(action=services.AUDIT_CLOSURE_SAVED).count() == 2
    services.delete_closure(short, actor=staff)
    assert _doc(AuditEntry.objects.get(action=services.AUDIT_CLOSURE_DELETED).before)["date"] == "2027-01-03"


# --- the Yandex check ---------------------------------------------------------------------------------------


def test_check_yandex_tariffs_needs_the_warehouse_point() -> None:
    result = services.check_yandex_tariffs(actor=None)
    assert not result.ok and result.error_code == services.TARIFFS_NOT_CONFIGURED
    assert "tariffs" not in integration_selectors.provider_data("yandex")
    assert _doc(AuditEntry.objects.get(action=services.AUDIT_TARIFFS_CHECKED).after)["ok"] is False


def test_check_yandex_tariffs_stores_the_classes_like_the_command() -> None:
    services.update_settings(WAREHOUSE, actor=None)
    staff = StaffUserFactory.create()
    result = services.check_yandex_tariffs(actor=staff)
    assert result.ok and result.mode == "fake" and result.tariffs
    data = integration_selectors.provider_data("yandex")
    assert data["tariffs"] == list(result.tariffs) and data["tariffs_mode"] == "fake"
    assert data["tariffs_point"] == [69.240562, 41.311081] and data["tariffs_checked_at"]
    entry = AuditEntry.objects.get(action=services.AUDIT_TARIFFS_CHECKED)
    assert entry.actor_id == staff.pk and _doc(entry.after)["tariffs"] == list(result.tariffs)


class _Port:
    def __init__(self, error: Exception | None = None, names: tuple[str, ...] = ()) -> None:
        self.error, self.names = error, names

    def tariffs(self, point: Any, **kwargs: Any) -> list[Tariff]:
        if self.error is not None:
            raise self.error
        return [Tariff(name=name) for name in self.names]


@pytest.mark.parametrize(
    ("error", "code"),
    [
        (YandexRejected("estimating.tariff.no_categories_in_zone", "Telefon +998901234567 noto‘g‘ri"), None),
        (YandexUnavailable("timeout"), services.TARIFFS_UNAVAILABLE),
    ],
)
def test_check_yandex_tariffs_reports_a_code_and_stores_nothing(
    monkeypatch: pytest.MonkeyPatch, error: Exception, code: str | None
) -> None:
    services.update_settings(WAREHOUSE, actor=None)
    integration_services.update_provider_data("yandex", {"tariffs": ["courier"], "tariffs_mode": "fake"})
    monkeypatch.setattr(services, "get_yandex_delivery", lambda scenario=None: _Port(error))
    result = services.check_yandex_tariffs(actor=None)
    expected = code or "estimating.tariff.no_categories_in_zone"
    assert not result.ok and result.error_code == expected and not result.tariffs
    assert integration_selectors.provider_data("yandex")["tariffs"] == ["courier"]  # untouched
    entry = AuditEntry.objects.get(action=services.AUDIT_TARIFFS_CHECKED)
    assert _doc(entry.after)["error_code"] == expected and "998901234567" not in str(entry.after)


# --- staff incidents ----------------------------------------------------------------------------------------


@override_settings(ALERT_WEBHOOK_URL="https://alerts.example.invalid/hook")
def test_open_incident_is_audited_and_an_urgent_one_alerts() -> None:
    order = OrderRowFactory.create(paid=True)
    staff = StaffUserFactory.create()
    issue_id, created = services.open_incident(
        order.pk, note="Noto‘g‘ri mahsulot qadoqlangan", urgent=False, actor_id=staff.pk
    )
    assert created and not OutboxMessage.objects.filter(topic="alert.send").exists()
    issue = DeliveryIssue.objects.get(pk=issue_id)
    assert (issue.kind, issue.code, issue.created_by_id) == ("incident", services.INCIDENT_CODE, staff.pk)
    again, created_again = services.open_incident(
        order.pk, note="Mahsulot yetishmaydi", urgent=True, actor_id=staff.pk
    )
    assert again == issue_id and not created_again
    issue.refresh_from_db()
    assert issue.urgent and issue.note == "Mahsulot yetishmaydi"
    audits = AuditEntry.objects.filter(action=services.AUDIT_ISSUE_OPENED).order_by("id")
    assert [_doc(entry.after)["created"] for entry in audits] == [True, False]
    alert = OutboxMessage.objects.get(topic="alert.send")
    assert alert.payload["event"] == "delivery.issue_opened"
    assert alert.payload["fields"] == {
        "order_id": order.pk,
        "kind": "incident",
        "code": services.INCIDENT_CODE,
    }
    with pytest.raises(ValueError):
        services.open_incident(order.pk, note="  ", urgent=False, actor_id=staff.pk)


# --- quote purge (beat delivery.purge_quotes) ---------------------------------------------------------------


def test_purge_quotes_task_deletes_unused_quotes_expired_a_day_ago() -> None:
    now = timeutils.now()
    old = DeliveryQuoteFactory.create(created_at=now - timedelta(days=2))
    recent = DeliveryQuoteFactory.create(created_at=now - timedelta(hours=2))
    used = DeliveryQuoteFactory.create(created_at=now - timedelta(days=3))
    order = OrderRowFactory.create(paid=True)
    DeliveryQuote.objects.filter(pk=used.pk).update(used_at=now - timedelta(days=3), order=order)
    assert tasks.purge_quotes() == 1
    assert set(DeliveryQuote.objects.values_list("pk", flat=True)) == {recent.pk, used.pk}
    assert not DeliveryQuote.objects.filter(pk=old.pk).exists()


def test_the_purge_runs_daily_in_beat() -> None:
    from config.celery import app

    entry = app.conf.beat_schedule["delivery-purge-quotes"]
    assert entry["task"] == "delivery.purge_quotes"
