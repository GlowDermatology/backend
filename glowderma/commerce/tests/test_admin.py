"""The commerce Django admin is read-only (design 03 §8): registrations, list pages and the order inlines."""

import pytest
from django.contrib import admin
from django.db import models
from django.test import Client, RequestFactory
from django.urls import reverse

from glowderma.accounts.tests.factories import StaffUserFactory
from glowderma.commerce.admin import OrderAdmin, OrderEventInline, OrderItemInline
from glowderma.commerce.models import Cart, CartItem, Order, OrderEvent, OrderItem, StockReservation
from glowderma.commerce.tests.factories import (
    CartItemFactory,
    OrderEventFactory,
    OrderFactory,
    StockReservationFactory,
)
from glowderma.core.admin import ReadOnlyAdmin

pytestmark = pytest.mark.django_db

MODELS = [Order, OrderItem, OrderEvent, StockReservation, Cart, CartItem]


@pytest.fixture
def super_client() -> Client:
    client = Client()
    client.force_login(StaffUserFactory.create(email="root@x.uz", is_staff=True, is_superuser=True))
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
    assert not model_admin.has_add_permission(request)
    assert not model_admin.has_change_permission(request)
    assert not model_admin.has_delete_permission(request)
    assert model_admin.list_display != ("__str__",)
    for field in model._meta.concrete_fields:
        if field.is_relation:
            assert field.name in model_admin.raw_id_fields, (model, field.name)


def test_order_admin_inlines_are_read_only() -> None:
    model_admin = admin.site._registry[Order]
    assert isinstance(model_admin, OrderAdmin)
    assert model_admin.inlines == (OrderItemInline, OrderEventInline)
    request = RequestFactory().get("/django-admin/")
    request.user = StaffUserFactory.create(is_staff=True)
    for inline in model_admin.get_inline_instances(request):
        assert not inline.has_add_permission(request, None)
        assert not inline.has_change_permission(request, None)
        assert not inline.has_delete_permission(request, None)
        assert tuple(inline.get_readonly_fields(request)) == inline.fields


def test_pages_render_for_a_superuser(super_client: Client) -> None:
    order = OrderFactory.create(cancelled=True)
    event = OrderEventFactory.create(order=order, type="order_created", status_after="awaiting_payment")
    reservation = StockReservationFactory.create(order=order)
    cart_item = CartItemFactory.create(cart__customer=order.customer)
    for model in MODELS:
        assert super_client.get(url(model, "changelist")).status_code == 200, model
    change = super_client.get(url(Order, "change", order.pk))
    assert change.status_code == 200
    content = change.content.decode()
    assert order.items.get().billz_id in content  # OrderItem inline
    assert "order_created" in content  # OrderEvent inline
    assert "Mijoz so‘rovi bilan" in content
    for model, pk in ((OrderEvent, event.pk), (StockReservation, reservation.pk), (CartItem, cart_item.pk)):
        assert super_client.get(url(model, "change", pk)).status_code == 200
    assert super_client.get(url(Order, "changelist") + f"?q={order.pk}").status_code == 200
    assert super_client.get(url(Order, "changelist") + "?status__exact=cancelled").status_code == 200
    assert super_client.get(url(Order, "add")).status_code == 403
    assert super_client.post(url(Order, "change", order.pk), {"status": "delivered"}).status_code == 403
    assert super_client.get(url(OrderEvent, "delete", event.pk)).status_code == 403
    order.refresh_from_db()
    assert order.status == "cancelled"
