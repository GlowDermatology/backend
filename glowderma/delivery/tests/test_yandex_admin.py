"""The Yandex Delivery pages of the ops console (plan §3.10): the editable settings page (a singleton written
through the audited services), the hours / special-day pages, the «Yandex bilan tekshirish» action and the
read-only claim, issue and quote pages, in the three console languages."""

from collections.abc import Iterator
from datetime import date, datetime, time
from decimal import Decimal
from typing import Any, cast

import pytest
from django import forms
from django.contrib import admin
from django.contrib.admin.models import LogEntry
from django.db import models
from django.test import Client, RequestFactory, override_settings
from django.urls import reverse

from glowderma.accounts.tests.factories import StaffUserFactory
from glowderma.accounts.tests.ops_helpers import ops_login
from glowderma.core import console_i18n
from glowderma.core.admin import ReadOnlyAdmin, ServiceBackedAdmin
from glowderma.core.models import AuditEntry
from glowderma.core.tests.console import confirm, messages_of
from glowderma.delivery import services
from glowderma.delivery.admin import DeliverySettingsAdmin
from glowderma.delivery.models import (
    CourierClaim,
    DeliveryIssue,
    DeliveryQuote,
    DeliverySettings,
    WarehouseClosure,
    WarehouseHours,
)
from glowderma.delivery.tests.factories import (
    DeliveryIssueFactory,
    DeliveryQuoteFactory,
    yandex_shipment,
)
from glowderma.integrations import selectors as integration_selectors
from glowderma.integrations import services as integration_services
from glowderma.integrations.yandex_delivery import fake as fake_yandex

pytestmark = pytest.mark.django_db

SETTINGS_URL = "/django-admin/delivery/deliverysettings/1/change/"
CHECK_URL = "/django-admin/delivery/deliverysettings/1/yandex-check/"
LANGUAGES = ("uz", "en", "ru")


@pytest.fixture(autouse=True)
def fake_reset() -> Iterator[None]:
    fake_yandex.reset()
    yield
    fake_yandex.reset()


@pytest.fixture
def console() -> Client:
    """A plain active ``is_staff`` user with a verified console session (never a superuser)."""
    client = Client()
    ops_login(client, StaffUserFactory.create(email="ops@x.uz", is_staff=True))
    return client


def url(model: type[models.Model], page: str, *args: object) -> str:
    return reverse(f"admin:{model._meta.app_label}_{model._meta.model_name}_{page}", args=args)


def _plain(value: Any) -> str:
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d %H:%M:%S")
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, time):
        return value.strftime("%H:%M")
    return "" if value is None else str(value)


def _context(response: Any) -> dict[str, Any]:
    context = response.context_data
    assert isinstance(context, dict)
    return context


def _doc(value: Any) -> dict[str, Any]:
    assert isinstance(value, dict)
    return value


def payload(response: Any, **changes: Any) -> dict[str, str]:
    """The change form as a browser submits it (every field with its rendered value), with ``changes`` on
    top (``True`` / ``False`` tick or clear a checkbox)."""
    form = _context(response)["adminform"].form
    data: dict[str, str] = {}
    values = {name: form[name].value() for name in form.fields}
    values.update(changes)
    for name, field in form.fields.items():
        value = values[name]
        if isinstance(field.widget, forms.CheckboxInput):
            if value:
                data[name] = "on"
            continue
        if isinstance(field.widget, forms.MultiWidget):
            parts: list[Any] = (
                value if isinstance(value, list) else list(field.widget.decompress(value) or [])
            )
            for index, part in enumerate(parts):
                data[f"{name}_{index}"] = _plain(part)
            continue
        data[name] = _plain(value)
    return data


# --- registrations ------------------------------------------------------------------------------------------


@pytest.mark.parametrize("model", [CourierClaim, DeliveryIssue, DeliveryQuote])
def test_the_yandex_records_are_read_only(model: type[models.Model]) -> None:
    model_admin = admin.site._registry[model]
    assert isinstance(model_admin, ReadOnlyAdmin)
    request = RequestFactory().get("/django-admin/")
    request.user = StaffUserFactory.create(is_staff=True)
    assert model_admin.has_view_permission(request)
    assert not model_admin.has_add_permission(request)
    assert not model_admin.has_change_permission(request)
    assert not model_admin.has_delete_permission(request)
    for field in model._meta.concrete_fields:
        if field.is_relation:
            assert field.name in (*model_admin.raw_id_fields, *model_admin.autocomplete_fields), field.name
    assert model_admin.check() == []


@pytest.mark.parametrize("model", [DeliverySettings, WarehouseHours, WarehouseClosure])
def test_the_settings_pages_are_service_backed(model: type[models.Model]) -> None:
    model_admin = admin.site._registry[model]
    assert isinstance(model_admin, ServiceBackedAdmin)
    assert model_admin.actions is None and model_admin.check() == []
    request = RequestFactory().get("/django-admin/")
    request.user = StaffUserFactory.create(is_staff=True)
    assert model_admin.has_change_permission(request)
    singleton = model is DeliverySettings
    assert model_admin.has_add_permission(request) is not singleton
    assert model_admin.has_delete_permission(request) is not singleton
    request.user = StaffUserFactory.create(is_staff=False)
    assert not model_admin.has_change_permission(request) and not model_admin.has_view_permission(request)


def test_the_quote_list_shows_no_address_or_pin() -> None:
    model_admin = admin.site._registry[DeliveryQuote]
    columns = [getattr(column, "source_field", column) for column in model_admin.list_display]
    assert not {"address_text", "latitude", "longitude"} & set(map(str, columns))
    request = RequestFactory().get("/django-admin/")
    request.user = StaffUserFactory.create(is_staff=True)
    assert not {"latitude", "longitude"} & set(model_admin.get_readonly_fields(request))


# --- pages in the three languages ---------------------------------------------------------------------------


@pytest.mark.parametrize("language", LANGUAGES)
def test_every_page_renders_for_a_plain_staff_user(console: Client, language: str) -> None:
    console.cookies[console_i18n.COOKIE_NAME] = language
    shipment = yandex_shipment(state="created", stage="courier_assigned")
    CourierClaim.objects.filter(pk=cast(int, shipment.claim_id)).update(
        courier_name="Fake Kuryer", courier_vehicle="Chevrolet Cobalt oq 01A777AA"
    )
    issue = DeliveryIssueFactory.create(order=shipment.order, urgent=True)
    quote = DeliveryQuoteFactory.create()
    hours = WarehouseHours.objects.create(weekday=0, opens_at=time(9), closes_at=time(18))
    closure = WarehouseClosure.objects.create(date=date(2026, 12, 31), note="Bayram")
    pages = [
        url(CourierClaim, "changelist"),
        url(CourierClaim, "change", cast(int, shipment.claim_id)),
        url(DeliveryIssue, "changelist"),
        url(DeliveryIssue, "change", issue.pk),
        url(DeliveryIssue, "changelist") + "?state__exact=open",
        url(DeliveryQuote, "changelist"),
        url(DeliveryQuote, "change", quote.pk),
        url(WarehouseHours, "changelist"),
        url(WarehouseHours, "change", hours.pk),
        url(WarehouseHours, "add"),
        url(WarehouseClosure, "changelist"),
        url(WarehouseClosure, "change", closure.pk),
        url(WarehouseClosure, "add"),
        SETTINGS_URL,
        CHECK_URL,
        "/django-admin/delivery/shipment/" + f"{shipment.pk}/change/",
        "/django-admin/delivery/shipment/?provider__exact=yandex&stage__exact=courier_assigned",
    ]
    for page in pages:
        response = console.get(page)
        assert response.status_code == 200, (page, response.status_code)
    claim_page = console.get(url(CourierClaim, "change", cast(int, shipment.claim_id))).content.decode()
    assert "Fake Kuryer" in claim_page
    shipment_page = console.get(f"/django-admin/delivery/shipment/{shipment.pk}/change/").content.decode()
    if language == "uz":
        assert "Kuryer biriktirildi" in shipment_page  # the stage column


def test_the_settings_list_opens_the_one_row(console: Client) -> None:
    response = console.get(url(DeliverySettings, "changelist"))
    assert response.status_code == 302 and response["Location"] == SETTINGS_URL
    assert console.get(url(DeliverySettings, "add")).status_code == 403
    assert console.get(url(DeliverySettings, "delete", 1)).status_code == 403
    assert DeliverySettings.objects.filter(pk=1).exists()


def test_the_console_is_closed_to_non_staff() -> None:
    client = Client()
    client.force_login(StaffUserFactory.create(is_staff=False))
    for page in (SETTINGS_URL, url(DeliverySettings, "changelist"), CHECK_URL):
        response = client.get(page)
        assert response.status_code in (302, 403), page
        if response.status_code == 302:
            assert "/login/" in response["Location"]


# --- saving through the services ----------------------------------------------------------------------------


def test_the_settings_page_saves_through_the_audited_service(console: Client) -> None:
    page = console.get(SETTINGS_URL)
    data = payload(
        page,
        warehouse_name="Markaziy ombor",
        warehouse_address="Toshkent, Yunusobod, 4-mavze, 12",
        warehouse_latitude="41.311081",
        warehouse_longitude="69.240562",
        contact_name="Malika",
        contact_phone="+998 90 123 45 67",
        prep_minutes="45",
        express_enabled=True,
    )
    response = console.post(SETTINGS_URL, data)
    assert response.status_code == 302, _context(response)["adminform"].form.errors
    row = DeliverySettings.objects.get(pk=1)
    assert row.contact_phone == "+998901234567" and row.prep_minutes == 45 and row.express_enabled
    assert row.warehouse_latitude == Decimal("41.311081") and row.updated_by is not None
    entry = AuditEntry.objects.get(action=services.AUDIT_SETTINGS_UPDATED)
    assert entry.actor_type == "staff" and entry.ip
    assert (
        _doc(entry.after)["contact_phone"] == {"changed": True}
        and _doc(entry.after)["prep_minutes"]["to"] == 45
    )
    assert LogEntry.objects.filter(object_id="1").exists()
    # an unchanged submit writes no audit row
    assert console.post(SETTINGS_URL, payload(console.get(SETTINGS_URL))).status_code == 302
    assert AuditEntry.objects.filter(action=services.AUDIT_SETTINGS_UPDATED).count() == 1


@pytest.mark.parametrize("language", LANGUAGES)
def test_an_invalid_pin_or_phone_is_refused_on_the_form(console: Client, language: str) -> None:
    console.cookies[console_i18n.COOKIE_NAME] = language
    data = payload(
        console.get(SETTINGS_URL),
        warehouse_latitude="55.755800",
        warehouse_longitude="37.617300",
        contact_phone="+7 999 123 45 67",
    )
    response = console.post(SETTINGS_URL, data)
    assert response.status_code == 200
    errors = _context(response)["adminform"].form.errors
    assert {"warehouse_latitude", "warehouse_longitude", "contact_phone"} <= set(errors)
    assert DeliverySettings.objects.get(pk=1).warehouse_latitude is None
    assert not AuditEntry.objects.filter(action=services.AUDIT_SETTINGS_UPDATED).exists()


@override_settings(YANDEX_DELIVERY_MODE="live")
def test_live_mode_enables_a_method_only_after_yandex_offered_its_class(console: Client) -> None:
    refused = console.post(SETTINGS_URL, payload(console.get(SETTINGS_URL), standard_enabled=True))
    assert refused.status_code == 200
    assert "standard_enabled" in _context(refused)["adminform"].form.errors
    assert not DeliverySettings.objects.get(pk=1).standard_enabled
    integration_services.update_provider_data("yandex", {"tariffs": ["courier"], "tariffs_mode": "live"})
    saved = console.post(SETTINGS_URL, payload(console.get(SETTINGS_URL), standard_enabled=True))
    assert saved.status_code == 302
    assert DeliverySettings.objects.get(pk=1).standard_enabled
    express = console.post(SETTINGS_URL, payload(console.get(SETTINGS_URL), express_enabled=True))
    assert express.status_code == 200 and "express_enabled" in _context(express)["adminform"].form.errors


def test_hours_and_special_days_are_written_through_the_services(console: Client) -> None:
    added = console.post(
        url(WarehouseHours, "add"), {"weekday": "2", "opens_at": "09:00", "closes_at": "18:00"}
    )
    assert added.status_code == 302
    hours = WarehouseHours.objects.get(weekday=2)
    assert AuditEntry.objects.get(action=services.AUDIT_HOURS_SAVED).entity_id == str(hours.pk)
    backwards = console.post(
        url(WarehouseHours, "change", hours.pk), {"weekday": "2", "opens_at": "18:00", "closes_at": "09:00"}
    )
    assert backwards.status_code == 200 and _context(backwards)["adminform"].form.errors
    assert WarehouseHours.objects.get(pk=hours.pk).opens_at == time(9)
    closure = console.post(
        url(WarehouseClosure, "add"),
        {"date": "2026-12-31", "opens_at": "", "closes_at": "", "note": "Bayram"},
    )
    assert closure.status_code == 302
    holiday = WarehouseClosure.objects.get(date=date(2026, 12, 31))
    assert holiday.created_by is not None and holiday.opens_at is None
    deleted = console.post(url(WarehouseClosure, "delete", holiday.pk), {"post": "yes"})
    assert deleted.status_code == 302 and not WarehouseClosure.objects.exists()
    assert AuditEntry.objects.filter(action=services.AUDIT_CLOSURE_DELETED).count() == 1
    page = console.get(SETTINGS_URL).content.decode()
    assert "09:00–18:00" in page  # the week on the settings page


# --- «Yandex bilan tekshirish» ------------------------------------------------------------------------------


def _configure() -> None:
    services.update_settings(
        {
            "warehouse_address": "Toshkent, Yunusobod, 4-mavze, 12",
            "warehouse_latitude": Decimal("41.311081"),
            "warehouse_longitude": Decimal("69.240562"),
        },
        actor=None,
    )


def test_the_check_action_asks_yandex_only_on_the_confirmed_post(console: Client) -> None:
    _configure()
    page = console.get(SETTINGS_URL).content.decode()
    assert "yandex-check" in page  # the detail button
    assert console.get(CHECK_URL).status_code == 200
    assert not AuditEntry.objects.filter(action=services.AUDIT_TARIFFS_CHECKED).exists()  # a GET never acts
    response = confirm(console, CHECK_URL)
    assert response.status_code == 302 and response["Location"] == SETTINGS_URL
    ((level, text),) = messages_of(response)
    assert level == "success" and "courier" in text
    assert "courier" in integration_selectors.provider_data("yandex")["tariffs"]
    assert AuditEntry.objects.get(action=services.AUDIT_TARIFFS_CHECKED).actor_type == "staff"
    assert "courier" in console.get(SETTINGS_URL).content.decode()  # the stored answer on the page


def test_the_check_action_reports_a_missing_warehouse_and_a_refusal_by_code(
    console: Client, monkeypatch: pytest.MonkeyPatch
) -> None:
    ((level, text),) = messages_of(confirm(console, CHECK_URL))
    assert level == "warning"
    _configure()

    def refuse(**kwargs: Any) -> services.TariffCheck:
        return services.TariffCheck(ok=False, mode="live", error_code="address_not_found")

    monkeypatch.setattr(services, "check_yandex_tariffs", refuse)
    level, text = messages_of(confirm(console, CHECK_URL))[-1]
    assert level == "error" and "address_not_found" in text


def test_the_check_action_runs_outside_a_transaction() -> None:
    """``ops_action`` wraps its call in a transaction; the Yandex check is its own view (no network call
    while a database transaction is open)."""
    method = DeliverySettingsAdmin.yandex_check
    assert vars(method)["url_path"] == "yandex-check" and vars(method)["allowed_permissions"] == ("ops",)
    assert "_run" not in method.__code__.co_names
