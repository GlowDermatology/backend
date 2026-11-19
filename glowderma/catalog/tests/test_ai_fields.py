"""feat/ai-assistant catalog additions: ``texts.*.short_description``, image alt text (``PATCH`` of an image,
``ProductInput.image_alt``), the ``ProductInput.expected`` check under the row lock (409 ``product_changed``),
the alt text in the SEO checklist and in the customer product detail."""

from __future__ import annotations

import threading
import time
from typing import Any

import pytest
from django.db import connection, transaction
from rest_framework.exceptions import NotFound
from rest_framework.test import APIClient

from glowderma.accounts.models import StaffUser
from glowderma.catalog import selectors, seo, services
from glowderma.catalog.models import Product, ProductImage
from glowderma.catalog.tests.factories import ProductFactory, ProductImageFactory, ProductStockFactory
from glowderma.catalog.tests.helpers import errors_of
from glowderma.core import enums
from glowderma.core.models import AuditEntry
from glowderma.customers.tests.p1_support import app_client

pytestmark = pytest.mark.django_db

PRODUCTS = "/api/v1/admin/products/"


def image_url(image: ProductImage) -> str:
    return f"{PRODUCTS}{image.product_id}/images/{image.pk}/"


# --- short description --------------------------------------------------------------------------------------


def test_short_description_round_trip_and_audit(staff_client: APIClient) -> None:
    product = ProductFactory.create(status="hidden")
    response = staff_client.patch(
        f"{PRODUCTS}{product.pk}/",
        {"texts": {"ru": {"short_description": "Лёгкая сыворотка."}}},
        format="json",
    )
    assert response.status_code == 200, response.json()
    texts = response.json()["texts"]
    assert texts["ru"]["short_description"] == "Лёгкая сыворотка."
    assert texts["uz"]["short_description"] == "" and texts["en"]["short_description"] == ""
    product.refresh_from_db()
    assert product.short_description_ru == "Лёгкая сыворотка."
    entry = AuditEntry.objects.filter(action="product.updated").latest("id")
    assert entry.after == {"texts.ru.short_description": "Лёгкая сыворотка."}


def test_short_description_limit(staff_client: APIClient) -> None:
    product = ProductFactory.create(status="hidden")
    response = staff_client.patch(
        f"{PRODUCTS}{product.pk}/", {"texts": {"uz": {"short_description": "x" * 301}}}, format="json"
    )
    assert response.status_code == 400
    assert errors_of(response)["texts.uz.short_description"][0] == "max_length"


# --- image alt text -----------------------------------------------------------------------------------------


def test_image_patch_merges_alt_leaves(staff_client: APIClient, staff: StaffUser) -> None:
    image = ProductImageFactory.create(product=ProductFactory.create(status="hidden"))
    before = Product.objects.get(pk=image.product_id)
    response = staff_client.patch(image_url(image), {"alt": {"uz": "  Serum flakoni  "}}, format="json")
    assert response.status_code == 200, response.json()
    assert response.json()["alt"] == {"uz": "Serum flakoni", "ru": "", "en": ""}
    response = staff_client.patch(image_url(image), {"alt": {"ru": "Флакон"}}, format="json")
    assert response.json()["alt"] == {"uz": "Serum flakoni", "ru": "Флакон", "en": ""}
    image.refresh_from_db()
    assert (image.alt_uz, image.alt_ru, image.alt_en) == ("Serum flakoni", "Флакон", "")
    after = Product.objects.get(pk=image.product_id)
    assert after.updated_at > before.updated_at and after.last_change_at == before.last_change_at
    entries = list(AuditEntry.objects.filter(action="product.image_updated").order_by("id"))
    assert [entry.after for entry in entries] == [
        {f"images.{image.pk}.alt.uz": "Serum flakoni"},
        {f"images.{image.pk}.alt.ru": "Флакон"},
    ]
    detail = staff_client.get(f"{PRODUCTS}{image.product_id}/").json()
    assert detail["images"][0]["alt"] == {"uz": "Serum flakoni", "ru": "Флакон", "en": ""}


def test_image_patch_without_change_writes_no_audit(staff_client: APIClient) -> None:
    image = ProductImageFactory.create(product=ProductFactory.create(status="hidden"), alt_uz="A")
    response = staff_client.patch(image_url(image), {"alt": {"uz": "A"}}, format="json")
    assert response.status_code == 200
    assert not AuditEntry.objects.filter(action="product.image_updated").exists()


def test_image_patch_validation_and_404(staff_client: APIClient) -> None:
    image = ProductImageFactory.create(product=ProductFactory.create(status="hidden"))
    other = ProductFactory.create(status="hidden")
    response = staff_client.patch(image_url(image), {"alt": {"en": "x" * 161}}, format="json")
    assert response.status_code == 400 and errors_of(response)["alt.en"][0] == "max_length"
    response = staff_client.patch(image_url(image), {}, format="json")
    assert response.status_code == 400 and errors_of(response)["alt"][0] == "required"
    response = staff_client.patch(
        f"{PRODUCTS}{other.pk}/images/{image.pk}/", {"alt": {"uz": "a"}}, format="json"
    )
    assert response.status_code == 404


def test_update_product_sets_image_alt_with_the_texts(staff: StaffUser) -> None:
    product = ProductFactory.create(status="hidden")
    image = ProductImageFactory.create(product=product)
    services.update_product(
        product.pk,
        services.ProductInput(
            texts={"en": {"short_description": "Light serum."}}, image_alt={image.pk: {"en": " Bottle "}}
        ),
        actor=staff,
    )
    image.refresh_from_db()
    assert image.alt_en == "Bottle"
    entry = AuditEntry.objects.filter(action="product.updated").latest("id")
    assert entry.after == {
        "texts.en.short_description": "Light serum.",
        f"images.{image.pk}.alt.en": "Bottle",
    }
    with pytest.raises(NotFound):
        services.update_product(
            product.pk, services.ProductInput(image_alt={image.pk + 999: {"uz": "x"}}), actor=staff
        )


# --- expected values under the row lock ---------------------------------------------------------------------


def test_expected_values_match_then_write(staff: StaffUser) -> None:
    product = ProductFactory.create(status="hidden", name_ru="")
    image = ProductImageFactory.create(product=product)
    updated = services.update_product(
        product.pk,
        services.ProductInput(
            texts={"ru": {"name": "Сыворотка"}},
            expected={"texts.ru.name": "", f"images.{image.pk}.alt.uz": ""},
        ),
        actor=staff,
    )
    assert updated.name_ru == "Сыворотка"


@pytest.mark.parametrize("key", ["texts.ru.name", "images.{image}.alt.uz", "images.999999.alt.uz"])
def test_expected_mismatch_is_product_changed_and_writes_nothing(staff: StaffUser, key: str) -> None:
    product = ProductFactory.create(status="hidden", name_ru="Изменено")
    image = ProductImageFactory.create(product=product, alt_uz="bor")
    key = key.format(image=image.pk)
    before = services.product_snapshot(Product.objects.get(pk=product.pk))
    with pytest.raises(services.ProductChanged) as caught:
        services.update_product(
            product.pk,
            services.ProductInput(texts={"uz": {"description": "Yangi"}}, expected={key: ""}),
            actor=staff,
        )
    assert caught.value.keys == [key]
    assert caught.value.status_code == 409 and caught.value.get_codes() == "product_changed"
    assert services.product_snapshot(Product.objects.get(pk=product.pk)) == before
    assert not AuditEntry.objects.filter(action="product.updated").exists()


@pytest.mark.django_db(transaction=True)
def test_expected_is_checked_after_a_concurrent_write_commits(staff: StaffUser) -> None:
    product = ProductFactory.create(status="hidden", name_ru="")
    locked = threading.Event()
    outcome: list[Any] = []

    def editor() -> None:
        try:
            with transaction.atomic():
                row = Product.objects.select_for_update().get(pk=product.pk)
                row.name_ru = "Ручная правка"
                row.save(update_fields=["name_ru"])
                locked.set()
                time.sleep(0.5)  # the assistant's apply waits on the row lock meanwhile
        finally:
            connection.close()

    def assistant_apply() -> None:
        try:
            assert locked.wait(10)
            services.update_product(
                product.pk,
                services.ProductInput(texts={"ru": {"name": "ИИ-вариант"}}, expected={"texts.ru.name": ""}),
                actor=staff,
            )
            outcome.append("written")
        except services.ProductChanged as exc:
            outcome.append(exc.keys)
        except BaseException as exc:  # noqa: BLE001 - surfaced to the main thread below
            outcome.append(exc)
        finally:
            connection.close()

    threads = [threading.Thread(target=editor), threading.Thread(target=assistant_apply)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(10)
        assert not thread.is_alive()
    assert outcome == [["texts.ru.name"]]
    assert Product.objects.get(pk=product.pk).name_ru == "Ручная правка"


# --- SEO checklist ------------------------------------------------------------------------------------------


def codes(product: Product) -> set[str]:
    loaded = selectors.get_product(product.pk)
    assert loaded is not None
    images = list(loaded.images.all())
    return {
        issue.code
        for issue in seo.product_issues(
            loaded, image_count=len(images), images_without_alt=sum(1 for i in images if not i.alt_uz)
        )
    }


def test_alt_fallback_warning_only_for_images_without_their_own_alt() -> None:
    product = ProductFactory.create(status="hidden", name_uz="")
    image = ProductImageFactory.create(product=product)
    assert enums.SeoIssueCode.ALT_TEXT_FALLBACK in codes(product)
    ProductImage.objects.filter(pk=image.pk).update(alt_uz="Serum flakoni")
    assert enums.SeoIssueCode.ALT_TEXT_FALLBACK not in codes(product)
    # the old call shape (no count) keeps the old meaning: every image falls back
    issues = seo.product_issues(Product.objects.get(pk=product.pk), image_count=1)
    assert enums.SeoIssueCode.ALT_TEXT_FALLBACK in {issue.code for issue in issues}


def test_seo_backlog_counts_alt_fallback_by_alt_text() -> None:
    with_alt = ProductFactory.create(name_uz="")
    ProductImageFactory.create(product=with_alt, alt_uz="Bor")
    without = ProductFactory.create(name_uz="")
    ProductImageFactory.create(product=without)
    backlog = selectors.seo_backlog(limit=10)
    [entry] = [row for row in backlog.issues if row.code == enums.SeoIssueCode.ALT_TEXT_FALLBACK]
    assert entry.ids == [without.pk]


# --- customer product detail --------------------------------------------------------------------------------


def test_customer_detail_shows_short_description_and_alt() -> None:
    product = ProductFactory.create(
        name_uz="Serum", short_description_uz="Yengil serum.", short_description_ru=""
    )
    ProductStockFactory.create(product=product, quantity=3)
    first = ProductImageFactory.create(product=product, alt_uz="Flakon", alt_ru="Флакон")
    ProductImageFactory.create(product=product)
    response = app_client(HTTP_ACCEPT_LANGUAGE="ru").get(f"/api/v1/catalog/products/{product.pk}/")
    assert response.status_code == 200, response.json()
    body = response.json()
    assert body["short_description"] == "Yengil serum."  # ru empty → uz (customer contract §1.4)
    alts = {image["id"]: image["alt"] for image in body["images"]}
    assert alts[first.pk] == "Флакон"
    assert set(alts.values()) == {"Флакон", body["name"]}
