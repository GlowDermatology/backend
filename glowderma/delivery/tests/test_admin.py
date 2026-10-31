"""The delivery Django admin is read-only (design 02 §6): registrations, list pages and the event inline."""

import pytest
from django.contrib import admin
from django.db import models
from django.test import Client, RequestFactory
from django.urls import reverse

from glowderma.accounts.tests.factories import StaffUserFactory
from glowderma.accounts.tests.ops_helpers import ops_login
from glowderma.core.admin import ReadOnlyAdmin, column_field
from glowderma.delivery.admin import ShipmentAdmin, ShipmentEventInline
from glowderma.delivery.models import Shipment, ShipmentEvent
from glowderma.delivery.tests.factories import ShipmentEventFactory, ShipmentFactory, create_message_for

pytestmark = pytest.mark.django_db

MODELS = [Shipment, ShipmentEvent]


@pytest.fixture
def plain_staff_client() -> Client:
    """The audience ``ReadOnlyAdmin`` exists for: active ``is_staff``, no superuser flag, no model perms."""
    client = Client()
    ops_login(client, StaffUserFactory.create(email="ops@x.uz", is_staff=True))
    return client


def url(model: type[models.Model], page: str, *args: object) -> str:
    return reverse(f"admin:{model._meta.app_label}_{model._meta.model_name}_{page}", args=args)


@pytest.mark.parametrize("model", MODELS)
def test_models_registered_read_only(model: type[models.Model]) -> None:
    model_admin = admin.site._registry[model]
    assert isinstance(model_admin, ReadOnlyAdmin)
    request = RequestFactory().get("/django-admin/")
    request.user = StaffUserFactory.create(is_staff=True)
    assert model_admin.has_view_permission(request)
    assert model_admin.has_module_permission(request)
    assert not model_admin.has_add_permission(request)
    assert not model_admin.has_change_permission(request)
    assert not model_admin.has_delete_permission(request)
    assert model_admin.list_display != ("__str__",)
    for field in model._meta.concrete_fields:
        if field.is_relation:
            assert field.name in (*model_admin.raw_id_fields, *model_admin.autocomplete_fields), (
                model,
                field.name,
            )


def test_shipment_admin_as_designed() -> None:
    model_admin = admin.site._registry[Shipment]
    assert isinstance(model_admin, ShipmentAdmin)
    assert tuple(map(column_field, model_admin.list_display)) == (
        "id",
        "order",
        "state",
        "tracking_number",
        "tariff",
        "bts_status",
        "via",
        "attempts",
        "last_error_code",
        "last_attempt_at",
    )
    assert tuple(map(column_field, model_admin.list_filter)) == ("state", "via", "last_attempt_at")
    assert model_admin.search_fields == ("tracking_number", "=order__id")
    assert model_admin.inlines == (ShipmentEventInline,)
    request = RequestFactory().get("/django-admin/")
    request.user = StaffUserFactory.create(is_staff=True)
    (inline,) = model_admin.get_inline_instances(request)
    assert isinstance(inline, ShipmentEventInline)
    assert inline.has_view_permission(request, None)
    assert not inline.has_add_permission(request, None)
    assert not inline.has_change_permission(request, None)
    assert not inline.has_delete_permission(request, None)
    assert set(inline.fields) <= set(inline.get_readonly_fields(request))
    event_admin = admin.site._registry[ShipmentEvent]
    assert tuple(map(column_field, event_admin.list_display)) == (
        "at",
        "shipment",
        "kind",
        "bts_status",
        "via",
        "error_code",
    )
    assert tuple(map(column_field, event_admin.list_filter)) == ("kind", "via", "at")
    assert "payload_pretty" in event_admin.get_readonly_fields(request)


def test_pages_render_for_a_plain_staff_user(plain_staff_client: Client) -> None:
    shipment = ShipmentFactory.create(created=True, tracking_number="48213907")
    create_message_for(shipment, status="done")
    event = ShipmentEventFactory.create(shipment=shipment)
    ShipmentEventFactory.create(
        shipment=shipment, kind=ShipmentEvent.Kind.CREATE_FAILED, error_code="bts_rejected", bts_status=""
    )
    pending = ShipmentFactory.create(failed=True)
    create_message_for(pending)
    for model in MODELS:
        assert plain_staff_client.get(url(model, "changelist")).status_code == 200, model
    change = plain_staff_client.get(url(Shipment, "change", shipment.pk))
    assert change.status_code == 200
    content = change.content.decode()
    assert "48213907" in content
    assert "bts_rejected" in content  # ShipmentEventInline
    assert str(shipment.create_message_id) in content
    assert plain_staff_client.get(url(ShipmentEvent, "change", event.pk)).status_code == 200
    assert plain_staff_client.get(url(Shipment, "change", pending.pk)).status_code == 200
    assert plain_staff_client.get(url(Shipment, "changelist") + "?q=48213907").status_code == 200
    assert plain_staff_client.get(url(Shipment, "changelist") + f"?q={shipment.order_id}").status_code == 200
    assert plain_staff_client.get(url(Shipment, "changelist") + "?state__exact=failed").status_code == 200
    assert (
        plain_staff_client.get(url(ShipmentEvent, "changelist") + "?kind__exact=created").status_code == 200
    )
    assert plain_staff_client.get(url(Shipment, "add")).status_code == 403
    assert (
        plain_staff_client.post(url(Shipment, "change", shipment.pk), {"state": "failed"}).status_code == 403
    )
    assert plain_staff_client.get(url(ShipmentEvent, "delete", event.pk)).status_code == 403
    shipment.refresh_from_db()
    assert shipment.state == "created"


def test_inactive_staff_sees_nothing() -> None:
    inactive = Client()
    user = StaffUserFactory.create(email="gone@x.uz", is_staff=True, is_active=False)
    request = RequestFactory().get("/django-admin/")
    request.user = user
    model_admin = admin.site._registry[Shipment]
    assert not model_admin.has_view_permission(request)
    assert model_admin.get_inline_instances(request) == []
    assert inactive.get(url(Shipment, "changelist")).status_code in (302, 403)
