"""Design 01 §11: every catalog model is registered read-only with the listed columns."""

import pytest
from django.contrib import admin
from django.db import models
from django.test import Client, RequestFactory
from django.urls import reverse

from glowderma.accounts.tests.factories import StaffUserFactory
from glowderma.catalog.models import Banner, Category, Product, ProductImage, ProductStock
from glowderma.catalog.tests.factories import (
    BannerFactory,
    CategoryFactory,
    ProductFactory,
    ProductImageFactory,
    ProductStockFactory,
)
from glowderma.core.admin import ReadOnlyAdmin, column_field

pytestmark = pytest.mark.django_db

EXPECTED = {
    Category: ("slug", "name_uz", "position", "is_active"),
    Product: (
        "id",
        "billz_id",
        "name_uz",
        "billz_name",
        "category",
        "price",
        "status",
        "promo",
        "is_new_from_billz",
    ),
    ProductStock: ("product", "quantity", "synced_at"),
    ProductImage: ("product", "position", "content_type", "width", "height", "uploaded_by"),
    Banner: ("id", "title_uz", "link_kind", "is_active", "starts_at", "ends_at", "position"),
}


@pytest.mark.parametrize("model", list(EXPECTED))
def test_registered_read_only_with_design_columns(model: type[models.Model]) -> None:
    model_admin = admin.site._registry[model]
    assert isinstance(model_admin, ReadOnlyAdmin)
    assert tuple(map(column_field, model_admin.list_display)) == EXPECTED[model]
    request = RequestFactory().get("/django-admin/")
    request.user = StaffUserFactory.create(is_staff=True)
    assert model_admin.has_view_permission(request)
    assert not model_admin.has_add_permission(request)
    assert not model_admin.has_change_permission(request)
    assert not model_admin.has_delete_permission(request)
    assert model_admin.check() == []


def test_changelists_and_change_pages_render() -> None:
    client = Client()
    client.force_login(StaffUserFactory.create(is_staff=True, is_superuser=True))
    image = ProductImageFactory.create()
    rows = [
        CategoryFactory.create(),
        image.product,
        ProductStockFactory.create(product=image.product),
        image,
        BannerFactory.create(link_kind="product", link_product=ProductFactory.create()),
    ]
    for row in rows:
        meta = row._meta
        assert client.get(reverse(f"admin:{meta.app_label}_{meta.model_name}_changelist")).status_code == 200
        change = client.get(reverse(f"admin:{meta.app_label}_{meta.model_name}_change", args=[row.pk]))
        assert change.status_code == 200
        assert client.get(reverse(f"admin:{meta.app_label}_{meta.model_name}_add")).status_code == 403
    search = client.get(reverse("admin:catalog_product_changelist"), {"q": image.product.billz_id})
    assert search.status_code == 200 and image.product.billz_id.encode() in search.content
