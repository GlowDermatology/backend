"""commerce.0004 / 0005 and the OrderItem image path data step (plan §3 P2 models; the frontend's image
bug)."""

from __future__ import annotations

from importlib import import_module

import pytest
from django.apps import apps as global_apps
from django.test import override_settings

from glowderma.commerce.models import CartItem, OrderItem
from glowderma.commerce.tests.factories import CartItemFactory, OrderFactory, OrderItemFactory

image_step = import_module("glowderma.commerce.migrations.0005_orderitem_image_path")


@pytest.mark.parametrize(
    ("stored", "expected"),
    [
        ("http://127.0.0.1:8000/media/products/1/a.webp", "products/1/a.webp"),
        ("https://cdn.glowderma.uz/media/products/2/b.webp", "products/2/b.webp"),
        ("/media/products/3/c.webp", "products/3/c.webp"),
        ("products/4/d.webp", "products/4/d.webp"),
        ("", ""),
    ],
)
def test_media_path(stored: str, expected: str) -> None:
    assert image_step.media_path(stored) == expected


@override_settings(PUBLIC_MEDIA_BASE_URL="https://media.example.uz/assets/")
def test_media_path_strips_a_custom_base_path() -> None:
    assert image_step.media_path("https://media.example.uz/assets/products/5/e.webp") == "products/5/e.webp"


@override_settings(PUBLIC_MEDIA_BASE_URL="https://media.example.uz/m")
def test_media_path_matches_prefixes_on_a_segment_boundary_longest_first() -> None:
    # "/m" is a string prefix of "/media/…": it must not eat the "m" of "media"
    assert image_step.media_path("https://media.example.uz/media/products/1/a.webp") == "products/1/a.webp"
    assert image_step.media_path("https://media.example.uz/m/products/2/b.webp") == "products/2/b.webp"


@override_settings(MEDIA_URL="https://static.example.uz/files/")
def test_media_path_takes_the_path_of_an_absolute_media_url() -> None:
    assert image_step.media_path("https://static.example.uz/files/products/3/c.webp") == "products/3/c.webp"


@pytest.mark.django_db
def test_the_data_step_rewrites_existing_rows() -> None:
    order = OrderFactory.create(items=[])
    item = OrderItemFactory.create(order=order, image_url="http://127.0.0.1:8000/media/products/9/z.webp")
    kept = OrderItemFactory.create(order=OrderFactory.create(items=[]), image_url="")
    image_step.forwards(global_apps, None)
    assert OrderItem.objects.get(pk=item.pk).image_url == "products/9/z.webp"
    assert OrderItem.objects.get(pk=kept.pk).image_url == ""
    image_step.forwards(global_apps, None)  # idempotent
    assert OrderItem.objects.get(pk=item.pk).image_url == "products/9/z.webp"


@pytest.mark.django_db
def test_price_at_add_is_nullable_for_old_lines() -> None:
    line = CartItemFactory.create()
    assert CartItem.objects.get(pk=line.pk).price_at_add is None
