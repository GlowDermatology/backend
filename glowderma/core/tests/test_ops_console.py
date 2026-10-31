"""The Unfold ops console (glowderma.core.admin_site): the default site, theme hooks, column helpers."""

from typing import Any

import pytest
from django.conf import settings
from django.contrib import admin
from django.contrib.auth import get_user_model
from django.test import Client, RequestFactory, override_settings
from django.urls import reverse

from glowderma.core.admin import (
    ReadOnlyAdmin,
    column_field,
    customer_column,
    masked_email_column,
    masked_phone_column,
    money_column,
    som,
    status_column,
)
from glowderma.core.admin_site import OpsAdminSite, environment_callback, environment_title_prefix
from glowderma.core.models import OutboxMessage

pytestmark = pytest.mark.django_db


def staff_client(**flags: bool) -> Client:
    client = Client()
    client.force_login(
        get_user_model().objects.create_user("ops@x.uz", "Saffron!Harbor-Long72", is_staff=True)
    )
    return client


def test_the_default_site_is_the_ops_console() -> None:
    assert isinstance(admin.site._wrapped if hasattr(admin.site, "_wrapped") else admin.site, OpsAdminSite)
    assert admin.site.site_header == "Glow Derma Ops"
    assert settings.UNFOLD["SITE_TITLE"] == "Glow Derma Ops"


def test_single_role_staff_model_has_no_groups_and_no_stock_2fa_admin() -> None:
    from django.contrib.auth.models import Group
    from django_otp.plugins.otp_totp.models import TOTPDevice

    assert not admin.site.is_registered(Group)
    totp_admin = admin.site._registry[TOTPDevice]
    assert isinstance(totp_admin, ReadOnlyAdmin)
    assert "key" not in totp_admin.get_readonly_fields(RequestFactory().get("/"))


def test_every_registration_is_read_only_or_service_backed() -> None:
    request = RequestFactory().get("/django-admin/")
    request.user = get_user_model().objects.create_user("root@x.uz", "Saffron!Harbor-Long72", is_staff=True)
    for model, model_admin in admin.site._registry.items():
        if model._meta.app_label == "professionals":
            continue  # ServiceBackedAdmin: writes go through professionals.services
        assert isinstance(model_admin, ReadOnlyAdmin), model
        assert not model_admin.has_add_permission(request), model
        assert not model_admin.has_change_permission(request), model
        assert not model_admin.has_delete_permission(request), model


@pytest.mark.parametrize(
    ("label", "expected"), [("LOCAL", "info"), ("STAGING", "warning"), ("PRODUCTION", "danger")]
)
def test_environment_badge(label: str, expected: str) -> None:
    with override_settings(OPS_ENVIRONMENT_LABEL=label):
        assert environment_callback(RequestFactory().get("/")) == [label, expected]
        prefix = environment_title_prefix(RequestFactory().get("/"))
        assert prefix == ("" if label == "PRODUCTION" else f"[{label}]")


def test_index_renders_the_branded_console_with_sidebar_and_badge() -> None:
    OutboxMessage.objects.create(topic="billz.push_sale", payload={}, dedupe_key="dead-1", status="dead")
    page = staff_client().get(reverse("admin:index"))
    assert page.status_code == 200
    content = page.content.decode()
    assert "Glow Derma Ops" in content
    assert "ops/favicon.svg" in content and "ops/admin.css" in content
    assert reverse("admin:commerce_order_changelist") in content
    assert "Integratsiyalar va navbatlar" in content
    assert "sidebar-badge" in content  # the dead outbox message
    assert "LOCAL" in content


def test_login_page_is_unfold_styled_with_the_guarded_form() -> None:
    from glowderma.accounts.api.guard import GuardedAdminAuthenticationForm

    assert admin.site.login_form is GuardedAdminAuthenticationForm
    page = Client().get("/django-admin/login/")
    assert page.status_code == 200
    assert b"Glow Derma Ops" in page.content and b'name="username"' in page.content
    # Unfold's form posts no ``next``: a direct login still lands on the console, not /accounts/profile/
    get_user_model().objects.create_user("login@x.uz", "Saffron!Harbor-Long72", is_staff=True)
    answer = Client().post(
        "/django-admin/login/", {"username": "login@x.uz", "password": "Saffron!Harbor-Long72"}
    )
    assert answer.status_code == 302 and answer["Location"] == "/django-admin/"


class _Customer:
    pk = 7
    name = ""
    phone = "+998901234567"
    email = "malika@gmail.com"


class _Row:
    def __init__(self, **values: Any) -> None:
        self.__dict__.update(values)


def test_column_helpers() -> None:
    assert som(1250000) == "1 250 000 so‘m" and som(None) == "-"
    row: Any = _Row(total=99000, status="paid", customer=_Customer(), phone="+998901234567", email="a@b.uz")
    assert money_column("total", "Jami")(row) == "99 000 so‘m"
    column = status_column("status", "Holat", {"paid": "success"}, {"paid": "To‘langan"})
    assert column(row) == ("paid", "To‘langan")
    assert column.label == {"paid": "success"}  # type: ignore[attr-defined]
    assert status_column("status", "Holat", {})(_Row(status="")) == ("", "-")  # type: ignore[arg-type]
    assert masked_phone_column("phone")(row) == "+998 90 *** ** 67"
    assert masked_email_column("email")(row) == "a***@b.uz"
    assert customer_column()(row) == ["#7", "+998 90 *** ** 67"]
    assert [column_field(entry) for entry in ("id", column, ("created_at", object))] == [
        "id",
        "status",
        "created_at",
    ]
