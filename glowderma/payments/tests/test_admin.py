"""The payments Django admin is read-only (design 02 §6): registrations, list pages and the refund inline."""

import pytest
from django.contrib import admin
from django.db import models
from django.test import Client, RequestFactory
from django.urls import reverse

from glowderma.accounts.tests.factories import StaffUserFactory
from glowderma.core.admin import ReadOnlyAdmin, column_field
from glowderma.core.tests.admin_pages import rendered_field_names
from glowderma.payments.admin import PaymentAdmin, RefundInline
from glowderma.payments.models import Payment, Refund
from glowderma.payments.tests.factories import PaymentFactory, RefundFactory

pytestmark = pytest.mark.django_db

MODELS = [Payment, Refund]


@pytest.fixture
def plain_staff_client() -> Client:
    """The audience ``ReadOnlyAdmin`` exists for: active ``is_staff``, no superuser flag, no model perms."""
    client = Client()
    client.force_login(StaffUserFactory.create(email="ops@x.uz", is_staff=True))
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
    # provider_state is a JSON column: it renders through the pretty renderer, never as a form field
    assert "provider_state" in model_admin.get_exclude(request)
    assert "provider_state_pretty" in model_admin.get_readonly_fields(request)


def test_payment_admin_as_designed() -> None:
    model_admin = admin.site._registry[Payment]
    assert isinstance(model_admin, PaymentAdmin)
    assert tuple(map(column_field, model_admin.list_display)) == (
        "id",
        "order",
        "provider",
        "status",
        "amount",
        "provider_txn_id",
        "confirmed_at",
        "created_at",
    )
    assert tuple(map(column_field, model_admin.list_filter)) == ("provider", "status", "created_at")
    assert model_admin.search_fields == ("provider_txn_id", "=order__id")
    assert model_admin.inlines == (RefundInline,)
    request = RequestFactory().get("/django-admin/")
    request.user = StaffUserFactory.create(is_staff=True)
    (inline,) = model_admin.get_inline_instances(request)
    assert isinstance(inline, RefundInline)
    assert inline.has_view_permission(request, None)
    assert not inline.has_add_permission(request, None)
    assert not inline.has_change_permission(request, None)
    assert not inline.has_delete_permission(request, None)
    assert set(inline.fields) <= set(inline.get_readonly_fields(request))
    refund_admin = admin.site._registry[Refund]
    assert tuple(map(column_field, refund_admin.list_filter)) == ("state", "origin", "requested_at")
    assert refund_admin.search_fields == ("provider_refund_id", "=payment__id")


def test_pages_render_for_a_plain_staff_user(plain_staff_client: Client) -> None:
    payment = PaymentFactory.create(provider_txn_id="CLK-7741920385")
    refund = RefundFactory.create(failed=True, payment=payment, error_code="-31050")
    for model in MODELS:
        assert plain_staff_client.get(url(model, "changelist")).status_code == 200, model
    change = plain_staff_client.get(url(Payment, "change", payment.pk))
    assert change.status_code == 200
    content = change.content.decode()
    assert "CLK-7741920385" in content
    assert "-31050" in content  # RefundInline
    assert "provider_state_pretty" in rendered_field_names(change)
    assert plain_staff_client.get(url(Refund, "change", refund.pk)).status_code == 200
    assert plain_staff_client.get(url(Payment, "changelist") + f"?q={payment.order_id}").status_code == 200
    assert plain_staff_client.get(url(Payment, "changelist") + "?q=CLK-77").status_code == 200
    assert plain_staff_client.get(url(Payment, "changelist") + "?status__exact=paid").status_code == 200
    assert plain_staff_client.get(url(Refund, "changelist") + "?state__exact=failed&q=1").status_code == 200
    assert plain_staff_client.get(url(Payment, "add")).status_code == 403
    assert (
        plain_staff_client.post(url(Payment, "change", payment.pk), {"status": "failed"}).status_code == 403
    )
    assert plain_staff_client.get(url(Refund, "delete", refund.pk)).status_code == 403
    payment.refresh_from_db()
    assert payment.status == "paid"


def test_inactive_staff_sees_nothing() -> None:
    inactive = Client()
    user = StaffUserFactory.create(email="gone@x.uz", is_staff=True, is_active=False)
    request = RequestFactory().get("/django-admin/")
    request.user = user
    model_admin = admin.site._registry[Payment]
    assert not model_admin.has_view_permission(request)
    assert model_admin.get_inline_instances(request) == []
    assert inactive.get(url(Payment, "changelist")).status_code in (302, 403)
