"""Customer-side services the model invariants need: address sync, delete/reactivate scrub (decision A1)."""

from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from django.core.cache import cache
from django.core.files.base import ContentFile

from glowderma.accounts import services
from glowderma.accounts.models import Address, AuthSession, Customer, CustomerSocialIdentity, Device
from glowderma.accounts.tests.factories import (
    AddressFactory,
    CustomerFactory,
    CustomerSessionFactory,
    CustomerSocialIdentityFactory,
    DeviceFactory,
)
from glowderma.core import enums, timeutils

pytestmark = pytest.mark.django_db


def test_first_address_becomes_default_and_fills_address_text() -> None:
    customer = CustomerFactory.create()
    address = services.add_address(customer.pk, text="Toshkent, Chilonzor, 9-kvartal", label="Uy")
    customer.refresh_from_db()
    assert address.is_default
    assert customer.address_text == "Toshkent, Chilonzor, 9-kvartal"


def test_new_default_replaces_the_previous_one() -> None:
    customer = CustomerFactory.create()
    first = services.add_address(customer.pk, text="first")
    second = services.add_address(customer.pk, text="second", is_default=True)
    third = services.add_address(customer.pk, text="third")
    first.refresh_from_db()
    customer.refresh_from_db()
    assert (first.is_default, second.is_default, third.is_default) == (False, True, False)
    assert customer.address_text == "second"
    services.set_default_address(third)
    second.refresh_from_db()
    customer.refresh_from_db()
    assert not second.is_default and third.is_default
    assert customer.address_text == "third"
    services.set_default_address(third)  # idempotent
    assert Address.objects.filter(customer=customer, is_default=True).count() == 1


def test_remove_address_falls_back_to_the_most_recent_then_empty() -> None:
    customer = CustomerFactory.create()
    default = services.add_address(customer.pk, text="default")
    older = services.add_address(customer.pk, text="older")
    Address.objects.filter(pk=older.pk).update(updated_at=timeutils.now() - timedelta(days=1))
    newer = services.add_address(customer.pk, text="newer")
    services.remove_address(default)
    customer.refresh_from_db()
    assert customer.address_text == "newer"
    services.remove_address(newer)
    services.remove_address(older)
    customer.refresh_from_db()
    assert customer.address_text == ""


def test_add_address_requires_an_existing_customer() -> None:
    with pytest.raises(Customer.DoesNotExist):
        services.add_address(987654321, text="x")


def test_password_is_hashed_and_checked() -> None:
    customer = CustomerFactory.create()
    assert not services.check_customer_password(customer, "anything")
    services.set_customer_password(customer, "Saffron!Harbor-Long72")
    customer.refresh_from_db()
    assert customer.password.startswith("md5$")  # the test hasher; never the raw value
    assert customer.password_changed_at is not None
    assert services.check_customer_password(customer, "Saffron!Harbor-Long72")
    assert not services.check_customer_password(customer, "wrong")


def test_delete_customer_scrubs_and_revokes(
    django_capture_on_commit_callbacks: Any, settings: Any, tmp_path: Path
) -> None:
    settings.MEDIA_ROOT = tmp_path
    customer = CustomerFactory.create(name="Malika Karimova")
    customer.image.save("photo.jpg", ContentFile(b"not really a jpeg"), save=True)
    image_path = Path(customer.image.path)
    assert image_path.exists()
    services.set_customer_password(customer, "Saffron!Harbor-Long72")
    services.add_address(customer.pk, text="Toshkent")
    CustomerSocialIdentityFactory.create(customer=customer)
    device = DeviceFactory.create(customer=customer, fcm_token="tok", push_enabled=True)  # noqa: S106
    session = CustomerSessionFactory.create(customer=customer, device=device)
    closed = CustomerSessionFactory.create(
        customer=customer, revoked_at=timeutils.now(), revoke_reason="logout"
    )
    instant = timeutils.now()
    with django_capture_on_commit_callbacks(execute=True):
        result = services.delete_customer(customer)
    assert result is customer
    assert customer.status == enums.CustomerStatus.DELETED
    assert customer.deleted_at is not None and customer.deleted_at >= instant
    assert customer.name == "" and customer.address_text == "" and customer.password == ""
    assert customer.image.name == "" and not image_path.exists()
    assert not Address.objects.filter(customer=customer).exists()
    assert not CustomerSocialIdentity.objects.filter(customer=customer).exists()
    device.refresh_from_db()
    assert not device.is_active and not device.push_enabled and device.fcm_token == ""
    assert Device.objects.filter(customer=customer).exists()  # deactivated, not removed
    session.refresh_from_db()
    closed.refresh_from_db()
    assert session.revoked_at is not None and session.revoke_reason == "deactivated"
    assert closed.revoke_reason == "logout"  # already closed sessions keep their reason
    assert cache.get(f"auth:sid:{session.pk}") == "revoked:deactivated"
    assert customer.phone  # the row stays: loyalty/orders keep pointing at it


def test_delete_customer_is_idempotent() -> None:
    customer = CustomerFactory.create()
    services.delete_customer(customer)
    deleted_at = customer.deleted_at
    services.delete_customer(customer)
    customer.refresh_from_db()
    assert customer.deleted_at == deleted_at
    assert AuthSession.objects.filter(customer=customer).count() == 0


def test_reactivate_keeps_registered_at_and_records_the_date() -> None:
    customer = CustomerFactory.create(name="Malika Karimova")
    registered_at = customer.registered_at
    services.delete_customer(customer)
    services.reactivate_customer(customer, name="Malika K.")
    assert customer.status == enums.CustomerStatus.ACTIVE
    assert customer.deleted_at is None
    assert customer.reactivated_at is not None
    assert customer.registered_at == registered_at
    assert customer.name == "Malika K."
    reactivated_at = customer.reactivated_at
    services.reactivate_customer(customer)  # idempotent
    assert customer.reactivated_at == reactivated_at


def test_address_factory_and_sync_agree() -> None:
    address = AddressFactory.create(is_default=True)
    services.set_default_address(address)
    address.customer.refresh_from_db()
    assert address.customer.address_text == address.text
