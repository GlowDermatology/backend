"""Image variants of the customer image (spec 2026-11-13-image-variants): written with it, removed with it."""

from io import BytesIO

import pytest
from django.core.files.storage import default_storage
from PIL import Image

from glowderma.accounts import services
from glowderma.accounts.models import Customer
from glowderma.core.media import variant_name

pytestmark = pytest.mark.django_db

PHONE = "+998905556677"


def png(colour: str) -> bytes:
    output = BytesIO()
    Image.new("RGB", (640, 480), colour).save(output, format="PNG")
    return output.getvalue()


def names(original: str) -> list[str]:
    return [original, variant_name(original, "thumbnail"), variant_name(original, "medium")]


def test_set_replace_and_clear(django_capture_on_commit_callbacks: object) -> None:
    customer = Customer.objects.create(phone=PHONE, name="Malika")
    first = services.set_customer_image(customer, "aaaaaaaaaaaaaaaa.png", png("red"))
    assert all(default_storage.exists(name) for name in names(first))
    with django_capture_on_commit_callbacks(execute=True):  # type: ignore[operator]
        second = services.set_customer_image(customer, "bbbbbbbbbbbbbbbb.png", png("blue"))
    assert all(default_storage.exists(name) for name in names(second))
    assert not any(default_storage.exists(name) for name in names(first))
    with django_capture_on_commit_callbacks(execute=True):  # type: ignore[operator]
        services.clear_customer_image(customer.pk)
    assert not any(default_storage.exists(name) for name in names(second))


def test_account_deletion_removes_the_variants(django_capture_on_commit_callbacks: object) -> None:
    customer = Customer.objects.create(phone=PHONE, name="Malika")
    stored = services.set_customer_image(customer, "cccccccccccccccc.png", png("green"))
    assert all(default_storage.exists(name) for name in names(stored))
    with django_capture_on_commit_callbacks(execute=True):  # type: ignore[operator]
        services.delete_customer(customer.pk)
    assert not any(default_storage.exists(name) for name in names(stored))
