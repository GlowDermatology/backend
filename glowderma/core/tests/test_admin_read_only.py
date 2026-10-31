"""The Django admin is read-only: active ``is_staff`` users may view, nobody may add, change or delete."""

from typing import Any

import pytest
from django.contrib import admin
from django.contrib.admin.exceptions import AlreadyRegistered
from django.contrib.auth import get_user_model
from django.contrib.auth.models import AnonymousUser
from django.db import models
from django.test import Client, RequestFactory
from django.urls import reverse

from glowderma.core import audit
from glowderma.core.admin import ReadOnlyAdmin, register_read_only
from glowderma.core.models import AuditEntry, IdempotencyRecord, OutboxMessage

pytestmark = pytest.mark.django_db


def make_user(email: str, **flags: bool) -> Any:
    # core tests never import accounts (import-linter); the user model comes from settings.
    return get_user_model().objects.create_user(email, "Saffron!Harbor-Long72", **flags)


@pytest.fixture
def superuser() -> Any:
    return make_user("root@x.uz", is_staff=True, is_superuser=True)


@pytest.fixture
def super_client(superuser: Any) -> Client:
    client = Client()
    client.force_login(superuser)
    return client


@pytest.fixture
def entry() -> AuditEntry:
    return audit.record(
        action="test.changed", entity_type="test", entity_id="1", after={"name": "Yangi", "tiers": [1, 2]}
    )


def url(model: type[models.Model], page: str, *args: Any) -> str:
    return reverse(f"admin:{model._meta.app_label}_{model._meta.model_name}_{page}", args=args)


@pytest.mark.parametrize("model", [AuditEntry, OutboxMessage, IdempotencyRecord])
def test_core_models_registered_read_only(model: type[models.Model]) -> None:
    model_admin = admin.site._registry[model]
    assert isinstance(model_admin, ReadOnlyAdmin)
    request = RequestFactory().get("/django-admin/")
    request.user = make_user("root@x.uz", is_staff=True, is_superuser=True)
    assert model_admin.has_view_permission(request)
    assert not model_admin.has_add_permission(request)
    assert not model_admin.has_change_permission(request)
    assert not model_admin.has_delete_permission(request)
    assert model_admin.actions is None
    assert model_admin.list_display != ("__str__",)


def test_superuser_can_view_but_not_write(super_client: Client, entry: AuditEntry) -> None:
    assert super_client.get(url(AuditEntry, "changelist")).status_code == 200
    change = super_client.get(url(AuditEntry, "change", entry.pk))
    assert change.status_code == 200
    assert b'<pre class="gd-json">' in change.content
    assert b"&quot;name&quot;: &quot;Yangi&quot;" in change.content
    assert super_client.get(url(AuditEntry, "add")).status_code == 403
    assert super_client.post(url(AuditEntry, "add"), {"action": "x"}).status_code == 403
    assert super_client.post(url(AuditEntry, "change", entry.pk), {"action": "x"}).status_code == 403
    assert super_client.get(url(AuditEntry, "delete", entry.pk)).status_code == 403
    assert super_client.post(url(AuditEntry, "delete", entry.pk), {"post": "yes"}).status_code == 403
    bulk = super_client.post(
        url(AuditEntry, "changelist"), {"action": "delete_selected", "_selected_action": [entry.pk]}
    )
    assert bulk.status_code in (200, 302)
    assert AuditEntry.objects.filter(pk=entry.pk).exists()
    entry.refresh_from_db()
    assert entry.action == "test.changed"


def test_view_permission_requires_active_staff() -> None:
    model_admin = admin.site._registry[AuditEntry]
    request = RequestFactory().get("/django-admin/")
    request.user = make_user("plain@x.uz", is_staff=False)
    assert not model_admin.has_view_permission(request)
    request.user = make_user("inactive@x.uz", is_staff=True, is_active=False)
    assert not model_admin.has_view_permission(request)
    request.user = make_user("staff@x.uz", is_staff=True)
    assert model_admin.has_view_permission(request)
    request.user = AnonymousUser()
    assert not model_admin.has_view_permission(request)


def test_non_staff_user_cannot_open_admin_pages(entry: AuditEntry) -> None:
    client = Client()
    client.force_login(make_user("plain@x.uz", is_staff=False))
    response = client.get(url(AuditEntry, "changelist"))
    assert response.status_code == 302
    assert response["Location"].startswith("/django-admin/login/")


def test_readonly_fields_cover_every_field_with_pretty_json(entry: AuditEntry) -> None:
    model_admin = admin.site._registry[AuditEntry]
    request = RequestFactory().get("/django-admin/")
    fields = model_admin.get_readonly_fields(request, entry)
    assert "before_pretty" in fields and "after_pretty" in fields
    assert "before" not in fields and "after" not in fields
    assert {"action", "entity_type", "created_at", "ip"} <= set(fields)
    rendered = model_admin.after_pretty(entry)  # type: ignore[attr-defined]
    assert rendered.startswith('<pre class="gd-json">') and "&quot;tiers&quot;: [\n" in rendered
    assert model_admin.before_pretty(entry) == "-"  # type: ignore[attr-defined]
    assert model_admin.get_fields(request, entry) == list(fields)


def test_register_read_only_builds_subclass_with_options() -> None:
    """``register_read_only`` forwards its options to a ``ReadOnlyAdmin`` on the given site."""
    site = admin.AdminSite(name="test_read_only")
    register_read_only(OutboxMessage, site=site, list_display=("id", "topic"), search_fields=("topic",))
    model_admin = site._registry[OutboxMessage]
    assert isinstance(model_admin, ReadOnlyAdmin)
    assert type(model_admin) is not ReadOnlyAdmin  # an options subclass, as ``site.register`` builds
    assert model_admin.list_display == ("id", "topic")
    assert model_admin.search_fields == ("topic",)
    readonly = model_admin.get_readonly_fields(RequestFactory().get("/"))
    assert "payload_pretty" in readonly and "payload" not in readonly
    assert model_admin.check() == []
    assert set(site._registry) == {OutboxMessage}  # a fresh site, no phantom model in the app registry
    with pytest.raises(AlreadyRegistered):
        register_read_only(OutboxMessage, site=site)
    with pytest.raises(AlreadyRegistered):
        register_read_only(AuditEntry)  # the default site is ``admin.site``


def test_register_read_only_defaults_to_the_global_site() -> None:
    registered = admin.site._registry[OutboxMessage]
    assert isinstance(registered, ReadOnlyAdmin)
    assert registered.list_display[:2] == ("id", "topic")
    assert registered.ordering == ("-id",)


def test_plain_staff_user_can_navigate_to_the_changelist(entry: AuditEntry) -> None:
    """Brief: view permission for active ``is_staff`` users, reachable through the admin navigation."""
    client = Client()
    client.force_login(make_user("staff@x.uz", is_staff=True))
    model_admin = admin.site._registry[AuditEntry]
    request = RequestFactory().get("/django-admin/")
    request.user = make_user("other@x.uz", is_staff=True)
    assert model_admin.has_module_permission(request)
    index = client.get(reverse("admin:index"))
    assert index.status_code == 200
    assert url(AuditEntry, "changelist").encode() in index.content
    assert client.get(reverse("admin:app_list", kwargs={"app_label": "core"})).status_code == 200
    assert client.get(url(AuditEntry, "changelist")).status_code == 200
    assert client.get(url(AuditEntry, "change", entry.pk)).status_code == 200
    assert client.get(url(AuditEntry, "add")).status_code == 403
    assert client.post(url(AuditEntry, "change", entry.pk), {"action": "x"}).status_code == 403
    assert client.post(url(AuditEntry, "delete", entry.pk), {"post": "yes"}).status_code == 403
    entry.refresh_from_db()
    assert entry.action == "test.changed"


def test_module_permission_requires_active_staff() -> None:
    model_admin = admin.site._registry[AuditEntry]
    request = RequestFactory().get("/django-admin/")
    request.user = make_user("plain@x.uz", is_staff=False)
    assert not model_admin.has_module_permission(request)
    request.user = make_user("inactive@x.uz", is_staff=True, is_active=False)
    assert not model_admin.has_module_permission(request)
    request.user = AnonymousUser()
    assert not model_admin.has_module_permission(request)


def test_admin_checks_pass() -> None:
    assert admin.site.check(None) == []
