"""The customer page of the ops console: tabs with the customer's orders, loyalty and sessions, no secrets."""

import pytest
from django.contrib import admin
from django.test import Client
from django.urls import reverse

from glowderma.accounts.models import Customer
from glowderma.accounts.tests.factories import CustomerSessionFactory, DeviceFactory, StaffUserFactory
from glowderma.accounts.tests.ops_helpers import ops_login
from glowderma.commerce.tests.factories import OrderFactory
from glowderma.core.admin import ReadOnlyAdmin
from glowderma.customers.admin import CustomerAdmin

pytestmark = pytest.mark.django_db


def test_customer_page_shows_related_records_in_tabs_without_secrets() -> None:
    device = DeviceFactory.create(fcm_token="fcm-secret-token")  # noqa: S106 - a marker to look for
    customer = device.customer
    customer.password = "md5$salt$deadbeef"  # noqa: S105 - a fake hash shape
    customer.save(update_fields=["password"])
    order = OrderFactory.create(customer=customer)
    session = CustomerSessionFactory.create(customer=customer, previous_refresh_hash="p" * 64)
    model_admin = admin.site._registry[Customer]
    assert isinstance(model_admin, CustomerAdmin) and isinstance(model_admin, ReadOnlyAdmin)
    client = Client()
    ops_login(client, StaffUserFactory.create(is_staff=True))
    page = client.get(reverse("admin:accounts_customer_change", args=[customer.pk]))
    assert page.status_code == 200
    content = page.content.decode()
    for tab in ("Buyurtmalar", "Cashback", "Qurilmalar", "Sessiyalar", "Mijoz", "Kirish"):
        assert tab in content, tab
    assert reverse("admin:commerce_order_change", args=[order.pk]) in content
    for secret in ("deadbeef", "fcm-secret-token", session.refresh_hash, "pppppppp"):
        assert secret not in content
    listing = client.get(reverse("admin:accounts_customer_changelist"))
    assert listing.status_code == 200
    assert str(customer.phone) not in listing.content.decode()  # masked on the list


def test_the_customer_page_masks_the_contact_like_the_list_and_the_title() -> None:
    from glowderma.accounts.tests.factories import CustomerFactory
    from glowderma.core import timeutils
    from glowderma.core.pii import mask_email, mask_phone

    customer = CustomerFactory.create(email="malika.k@example.uz", email_verified_at=timeutils.now())
    client = Client()
    ops_login(client, StaffUserFactory.create(is_staff=True))
    content = client.get(reverse("admin:accounts_customer_change", args=[customer.pk])).content.decode()
    assert str(customer.phone) not in content and "malika.k@example.uz" not in content
    assert mask_phone(str(customer.phone)) in content and mask_email("malika.k@example.uz") in content
