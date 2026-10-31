"""The loyalty Django admin is read-only (design 02 §6): registrations, list/change pages for a plain staff
user, the tier and item inlines, and the hidden ``PosDevice.key_hash``."""

import pytest
from django.contrib import admin
from django.db import models
from django.test import Client, RequestFactory
from django.urls import reverse

from glowderma.accounts.tests.factories import StaffUserFactory
from glowderma.accounts.tests.ops_helpers import ops_login
from glowderma.core.admin import ReadOnlyAdmin
from glowderma.loyalty.admin import CashbackPolicyAdmin, CashbackTierInline, PurchaseAdmin, PurchaseItemInline
from glowderma.loyalty.models import (
    CashbackHold,
    CashbackPolicy,
    CashbackTier,
    LedgerEntry,
    LoyaltyAccount,
    PosDevice,
    Purchase,
    PurchaseItem,
)
from glowderma.loyalty.tests.factories import (
    CashbackHoldFactory,
    CashbackPolicyFactory,
    LedgerEntryFactory,
    PosDeviceFactory,
    PurchaseFactory,
)

pytestmark = pytest.mark.django_db

MODELS = [
    CashbackPolicy,
    CashbackTier,
    LoyaltyAccount,
    LedgerEntry,
    CashbackHold,
    Purchase,
    PurchaseItem,
    PosDevice,
]


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


def test_pos_device_key_hash_is_hidden() -> None:
    model_admin = admin.site._registry[PosDevice]
    assert isinstance(model_admin, ReadOnlyAdmin)
    request = RequestFactory().get("/django-admin/")
    request.user = StaffUserFactory.create(is_staff=True)
    assert "key_hash" in model_admin.get_exclude(request)
    assert "key_hash" not in model_admin.get_readonly_fields(request)
    assert "key_hash" not in tuple(model_admin.list_display)
    assert "key_hash" not in tuple(model_admin.search_fields or [])


def test_inlines_are_read_only() -> None:
    request = RequestFactory().get("/django-admin/")
    request.user = StaffUserFactory.create(is_staff=True)
    for model, expected in ((CashbackPolicy, CashbackTierInline), (Purchase, PurchaseItemInline)):
        model_admin = admin.site._registry[model]
        assert isinstance(model_admin, CashbackPolicyAdmin | PurchaseAdmin)
        assert model_admin.inlines == (expected,)
        instances = model_admin.get_inline_instances(request)
        assert [type(inline) for inline in instances] == [expected]  # kept for staff without model perms
        (inline,) = instances
        assert inline.has_view_permission(request, None)
        assert not inline.has_add_permission(request, None)
        assert not inline.has_change_permission(request, None)
        assert not inline.has_delete_permission(request, None)
        assert tuple(inline.get_readonly_fields(request)) == inline.fields


def test_pages_render_for_a_plain_staff_user(plain_staff_client: Client) -> None:
    policy = CashbackPolicyFactory.create(tiers=[(0, 20_000_000, "1.00"), (20_000_000, 30_000_000, "2.00")])
    entry = LedgerEntryFactory.create(policy=policy, adjustment=False)
    hold = CashbackHoldFactory.create(captured=True)
    purchase = PurchaseFactory.create(order__promo_subtotal=40_000, credit=1_450, policy=policy)
    device = PosDeviceFactory.create()
    for model in MODELS:
        assert plain_staff_client.get(url(model, "changelist")).status_code == 200, model
    change = plain_staff_client.get(url(CashbackPolicy, "change", policy.pk))
    assert change.status_code == 200
    content = change.content.decode()
    assert "20000000" in content and "30000000" in content  # CashbackTier inline
    assert "Superseded by" in content and "Effective to" in content  # derived columns (02 §1.1)
    change = plain_staff_client.get(url(Purchase, "change", purchase.pk))
    assert change.status_code == 200
    content = change.content.decode()
    assert purchase.items.first().name in content  # type: ignore[union-attr]  # PurchaseItem inline
    change = plain_staff_client.get(url(PosDevice, "change", device.pk))
    assert change.status_code == 200
    content = change.content.decode()
    assert device.key_prefix in content and device.key_hash not in content and "key_hash" not in content
    for model, pk in (
        (CashbackTier, policy.tiers.first().pk),  # type: ignore[union-attr]
        (LoyaltyAccount, entry.account_id),
        (LedgerEntry, entry.pk),
        (CashbackHold, hold.pk),
        (PurchaseItem, purchase.items.first().pk),  # type: ignore[union-attr]
    ):
        assert plain_staff_client.get(url(model, "change", pk)).status_code == 200, model
    assert plain_staff_client.get(url(LedgerEntry, "changelist") + f"?q={entry.order_id}").status_code == 200
    assert plain_staff_client.get(url(LedgerEntry, "changelist") + "?kind__exact=credit").status_code == 200
    assert plain_staff_client.get(url(Purchase, "changelist") + "?q=S-1").status_code == 200
    assert (
        plain_staff_client.get(url(CashbackHold, "changelist") + "?state__exact=captured").status_code == 200
    )
    assert plain_staff_client.get(url(CashbackPolicy, "add")).status_code == 403
    assert plain_staff_client.post(url(LoyaltyAccount, "change", entry.account_id), {"balance": 1}).status_code == 403  # fmt: skip  # noqa: E501
    assert plain_staff_client.get(url(LedgerEntry, "delete", entry.pk)).status_code == 403
    assert plain_staff_client.get(url(PosDevice, "delete", device.pk)).status_code == 403
    entry.account.refresh_from_db()
    assert entry.account.balance == entry.balance_after


def test_inactive_staff_sees_nothing() -> None:
    inactive = Client()
    user = StaffUserFactory.create(email="gone@x.uz", is_staff=True, is_active=False)
    request = RequestFactory().get("/django-admin/")
    request.user = user
    model_admin = admin.site._registry[CashbackPolicy]
    assert not model_admin.has_view_permission(request)
    assert model_admin.get_inline_instances(request) == []
    assert inactive.get(url(CashbackPolicy, "changelist")).status_code in (302, 403)
