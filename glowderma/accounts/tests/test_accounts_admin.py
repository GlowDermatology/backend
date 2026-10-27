"""Accounts models in the Django admin: read-only, and never a password hash or refresh hash."""

import pytest
from django.contrib import admin
from django.test import Client, RequestFactory
from django.urls import reverse

from glowderma.accounts.api import guard
from glowderma.accounts.models import AuthSession, SocialIdentity, StaffUser
from glowderma.accounts.tests.factories import AuthSessionFactory, StaffUserFactory
from glowderma.core.admin import ReadOnlyAdmin

pytestmark = pytest.mark.django_db


@pytest.fixture
def super_client() -> Client:
    client = Client()
    client.force_login(StaffUserFactory.create(email="root@x.uz", is_staff=True, is_superuser=True))
    return client


@pytest.mark.parametrize("model", [StaffUser, AuthSession, SocialIdentity])
def test_accounts_models_are_read_only(model: type[StaffUser | AuthSession | SocialIdentity]) -> None:
    model_admin = admin.site._registry[model]
    assert isinstance(model_admin, ReadOnlyAdmin)
    request = RequestFactory().get("/django-admin/")
    request.user = StaffUserFactory.create(is_staff=True, is_superuser=True)
    assert not model_admin.has_add_permission(request)
    assert not model_admin.has_change_permission(request)
    assert not model_admin.has_delete_permission(request)


def test_secrets_are_never_shown(super_client: Client, staff: StaffUser) -> None:
    session = AuthSessionFactory.create(staff=staff)
    request = RequestFactory().get("/django-admin/")
    assert "password" not in admin.site._registry[StaffUser].get_readonly_fields(request)
    assert "refresh_hash" not in admin.site._registry[AuthSession].get_readonly_fields(request)
    page = super_client.get(reverse("admin:accounts_staffuser_change", args=[staff.pk]))
    assert page.status_code == 200
    assert staff.password.encode() not in page.content
    assert b"field-password" not in page.content
    assert b"field-email" in page.content
    page = super_client.get(reverse("admin:accounts_authsession_change", args=[session.pk]))
    assert page.status_code == 200
    assert session.refresh_hash.encode() not in page.content
    assert b"field-refresh_hash" not in page.content
    for model in (StaffUser, AuthSession, SocialIdentity):
        assert (
            super_client.get(reverse(f"admin:accounts_{model._meta.model_name}_changelist")).status_code
            == 200
        )
        assert super_client.get(reverse(f"admin:accounts_{model._meta.model_name}_add")).status_code == 403
    assert (
        super_client.post(reverse("admin:accounts_staffuser_change", args=[staff.pk]), {}).status_code == 403
    )
    assert (
        super_client.post(reverse("admin:accounts_staffuser_delete", args=[staff.pk]), {}).status_code == 403
    )
    staff.refresh_from_db()
    assert staff.is_active


def test_guarded_login_form_is_wired_once_at_import() -> None:
    assert admin.site.login_form is guard.GuardedAdminAuthenticationForm
    assert "login_form" not in guard.guarded_admin_login.__code__.co_names


def test_guarded_login_accepts_a_staff_user() -> None:
    StaffUserFactory.create(email="root@x.uz", is_staff=True, is_superuser=True)
    response = Client().post(
        "/django-admin/login/",
        {"username": "ROOT@x.uz", "password": "Saffron!Harbor-Long72", "next": "/django-admin/"},
    )
    assert response.status_code == 302
    assert response["Location"] == "/django-admin/"
