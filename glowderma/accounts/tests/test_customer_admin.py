"""The customer-side accounts models in the Django admin: read-only, secrets excluded (design 01 §11)."""

import pytest
from django.contrib import admin
from django.db import models
from django.test import Client, RequestFactory
from django.urls import reverse

from glowderma.accounts.models import Address, AuthSession, Customer, CustomerSocialIdentity, Device, SmsCode
from glowderma.accounts.tests.factories import (
    AddressFactory,
    CustomerSessionFactory,
    CustomerSocialIdentityFactory,
    DeviceFactory,
    SmsCodeFactory,
    StaffUserFactory,
)
from glowderma.core.admin import ReadOnlyAdmin
from glowderma.core.tests.admin_pages import rendered_field_names

pytestmark = pytest.mark.django_db


@pytest.fixture
def super_client() -> Client:
    client = Client()
    client.force_login(StaffUserFactory.create(email="root@x.uz", is_staff=True, is_superuser=True))
    return client


@pytest.mark.parametrize("model", [Customer, Device, Address, SmsCode, CustomerSocialIdentity])
def test_customer_models_are_read_only(model: type[models.Model]) -> None:
    model_admin = admin.site._registry[model]
    assert isinstance(model_admin, ReadOnlyAdmin)
    request = RequestFactory().get("/django-admin/")
    request.user = StaffUserFactory.create(is_staff=True, is_superuser=True)
    assert model_admin.has_view_permission(request)
    assert not model_admin.has_add_permission(request)
    assert not model_admin.has_change_permission(request)
    assert not model_admin.has_delete_permission(request)
    assert model_admin.list_display != ("__str__",)


def test_secret_columns_are_excluded() -> None:
    request = RequestFactory().get("/django-admin/")
    assert "password" not in admin.site._registry[Customer].get_readonly_fields(request)
    assert "fcm_token" not in admin.site._registry[Device].get_readonly_fields(request)
    sms_fields = admin.site._registry[SmsCode].get_readonly_fields(request)
    assert "code_hmac" not in sms_fields and "verification_token_hash" not in sms_fields
    session_fields = admin.site._registry[AuthSession].get_readonly_fields(request)
    assert "refresh_hash" not in session_fields and "previous_refresh_hash" not in session_fields


def test_pages_render_without_secrets(super_client: Client) -> None:
    device = DeviceFactory.create(fcm_token="fcm-secret-token")  # noqa: S106 - a marker to look for
    customer = device.customer
    customer.password = "md5$salt$deadbeef"  # noqa: S105 - a fake hash shape for the page check
    customer.save(update_fields=["password"])
    AddressFactory.create(customer=customer)
    code = SmsCodeFactory.create(code_hmac="c" * 64, verification_token_hash="v" * 64)
    CustomerSocialIdentityFactory.create(customer=customer)
    session = CustomerSessionFactory.create(customer=customer, previous_refresh_hash="p" * 64)
    page = super_client.get(reverse("admin:accounts_customer_change", args=[customer.pk]))
    assert page.status_code == 200
    assert b"deadbeef" not in page.content and "phone" in rendered_field_names(page)
    assert "password" not in rendered_field_names(page)
    page = super_client.get(reverse("admin:accounts_device_change", args=[device.pk]))
    assert page.status_code == 200 and b"fcm-secret-token" not in page.content
    page = super_client.get(reverse("admin:accounts_smscode_change", args=[code.pk]))
    assert page.status_code == 200
    assert b"cccccccc" not in page.content and b"vvvvvvvv" not in page.content
    page = super_client.get(reverse("admin:accounts_authsession_change", args=[session.pk]))
    assert page.status_code == 200 and b"pppppppp" not in page.content
    for model in (Customer, Device, Address, SmsCode, CustomerSocialIdentity):
        name = model._meta.model_name
        assert super_client.get(reverse(f"admin:accounts_{name}_changelist")).status_code == 200
        assert super_client.get(reverse(f"admin:accounts_{name}_changelist") + "?q=99").status_code == 200
        assert super_client.get(reverse(f"admin:accounts_{name}_add")).status_code == 403
