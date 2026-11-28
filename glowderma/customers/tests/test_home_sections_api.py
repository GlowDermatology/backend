"""The admin «Bosh sahifa» (admin contract §2.21): ``GET home-sections/`` lists every landing section with its
picks and the automatic choice; ``PUT home-sections/{key}/`` replaces a section's picks (``[]`` = automatic),
refusing what customers could not see now, and audits ``home_section.saved``."""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from django.core.cache import cache
from rest_framework.test import APIClient

from glowderma.catalog.models import HomePick, Product
from glowderma.catalog.tests.factories import (
    CategoryFactory,
    ProductFactory,
    ProductImageFactory,
    ProductStockFactory,
)
from glowderma.commerce import selectors as commerce_selectors
from glowderma.core import enums
from glowderma.core.models import AuditEntry
from glowderma.professionals.models import HomeSpecialist
from glowderma.professionals.tests.factories import ProfessionalFactory

pytestmark = pytest.mark.django_db
LIST = "/api/v1/admin/home-sections/"
KEYS = ["spotlight", "new", "promo", "bestsellers", "categories", "specialists"]


@pytest.fixture(autouse=True)
def no_bestsellers() -> Any:
    cache.delete(commerce_selectors.BESTSELLERS_CACHE_KEY)
    yield
    cache.delete(commerce_selectors.BESTSELLERS_CACHE_KEY)


def product(*, image: bool = True, promo: bool = False, **fields: Any) -> Product:
    row = ProductFactory.create(promo=promo, **fields)
    ProductStockFactory.create(product=row, quantity=10)
    if image:
        ProductImageFactory.create(product=row)
    return row


def put(client: APIClient, key: str, ids: list[Any]) -> Any:
    return client.put(f"{LIST}{key}/", {"ids": ids}, format="json", HTTP_IDEMPOTENCY_KEY=str(uuid.uuid4()))


def section(client: APIClient, key: str) -> dict[str, Any]:
    response = client.get(LIST)
    assert response.status_code == 200, response.content
    found: dict[str, Any] = next(row for row in response.json()["results"] if row["key"] == key)
    return found


def ids(items: list[dict[str, Any]]) -> list[str]:
    return [item["id"] for item in items]


def errors(response: Any) -> list[tuple[str | None, str, str]]:
    return [(e["field"], e["code"], e["message"]) for e in response.json()["errors"]]


def test_the_list_has_every_section_in_the_landings_order_and_its_automatic_choice(
    staff_client: APIClient,
) -> None:
    newest = [product() for _ in range(3)]
    on_promo = product(promo=True)
    categories = CategoryFactory.create_batch(2)
    specialist = ProfessionalFactory.create()
    response = staff_client.get(LIST)
    assert response.status_code == 200, response.content
    rows = response.json()["results"]
    assert [row["key"] for row in rows] == KEYS
    assert {row["key"]: (row["manual"], row["limit"], row["picks"]) for row in rows} == {
        "spotlight": (False, 6, []),
        "new": (False, 12, []),
        "promo": (False, 12, []),
        "bestsellers": (False, 12, []),
        "categories": (False, 12, []),
        "specialists": (False, 8, []),
    }
    by_key = {row["key"]: row for row in rows}
    assert ids(by_key["new"]["automatic"]) == [str(on_promo.pk), *(str(row.pk) for row in reversed(newest))]
    assert ids(by_key["promo"]["automatic"]) == [str(on_promo.pk)]
    assert by_key["bestsellers"]["automatic"] == []  # no cached bestsellers (and fewer than 4 hide anyway)
    assert set(ids(by_key["spotlight"]["automatic"])) == {str(row.pk) for row in [*newest, on_promo]}
    assert ids(by_key["specialists"]["automatic"]) == [str(specialist.pk)]
    product_categories = {row.category for row in [*newest, on_promo] if row.category is not None}
    category_slugs = {row.slug for row in product_categories} | {row.slug for row in categories}
    assert set(ids(by_key["categories"]["automatic"])) == category_slugs
    item = by_key["promo"]["automatic"][0]
    assert item["name"] == on_promo.name_uz and item["issue"] is None
    assert item["image_url"].endswith(".webp")


def test_saving_a_section_replaces_its_picks_audits_and_an_empty_list_makes_it_automatic(
    staff_client: APIClient,
) -> None:
    first, second = product(), product()
    response = put(staff_client, "spotlight", [str(second.pk), str(first.pk)])
    assert response.status_code == 200, response.content
    body = response.json()
    assert (body["key"], body["manual"]) == ("spotlight", True)
    assert ids(body["picks"]) == [str(second.pk), str(first.pk)]
    assert [item["issue"] for item in body["picks"]] == [None, None]
    assert list(HomePick.objects.values_list("product_id", "position")) == [(second.pk, 0), (first.pk, 1)]
    entry = AuditEntry.objects.get(action="home_section.saved")
    assert (entry.entity_type, entry.entity_id) == ("home_section", "spotlight")
    assert (entry.before, entry.after) == ({"ids": []}, {"ids": [str(second.pk), str(first.pk)]})
    response = put(staff_client, "spotlight", [])
    assert response.status_code == 200, response.content
    assert (response.json()["manual"], response.json()["picks"]) == (False, [])
    assert not HomePick.objects.exists()


def test_categories_by_slug_and_specialists_by_id(staff_client: APIClient) -> None:
    acne, oily = CategoryFactory.create_batch(2)
    first, second = ProfessionalFactory.create_batch(2)
    response = put(staff_client, "categories", [oily.slug, acne.slug])
    assert response.status_code == 200, response.content
    assert ids(response.json()["picks"]) == [oily.slug, acne.slug]
    response = put(staff_client, "specialists", [str(second.pk), str(first.pk)])
    assert response.status_code == 200, response.content
    assert ids(response.json()["picks"]) == [str(second.pk), str(first.pk)]
    assert list(HomeSpecialist.objects.values_list("professional_id", flat=True)) == [second.pk, first.pk]
    assert AuditEntry.objects.filter(action="home_section.saved").count() == 2


@pytest.mark.parametrize(
    ("key", "make", "message"),
    [
        ("new", lambda: product(status=enums.ProductStatus.HIDDEN), "saytda ko‘rinmaydi."),
        ("spotlight", lambda: product(image=False), "rasmsiz: karuselga rasmi bor mahsulot qo‘ying."),
        ("promo", lambda: product(promo=False), "aksiyada emas."),
    ],
)
def test_a_product_customers_could_not_see_there_is_refused(
    staff_client: APIClient, key: str, make: Any, message: str
) -> None:
    good, bad = product(promo=True), make()
    response = put(staff_client, key, [str(good.pk), str(bad.pk)])
    assert response.status_code == 400, response.content
    assert errors(response) == [("ids", "home_pick_unavailable", f"«{bad.name_uz}» {message}")]
    assert not HomePick.objects.exists()


def test_an_inactive_category_and_an_unpublished_specialist_are_refused(staff_client: APIClient) -> None:
    hidden = CategoryFactory.create(is_active=False)
    response = put(staff_client, "categories", [hidden.slug])
    assert errors(response) == [("ids", "home_pick_unavailable", f"«{hidden.name_uz}» saytda ko‘rinmaydi.")]
    draft = ProfessionalFactory.create(is_active=False)
    response = put(staff_client, "specialists", [str(draft.pk)])
    assert errors(response) == [
        (
            "ids",
            "home_pick_unavailable",
            f"«{draft.name}» saytda ko‘rinmaydi: profil e’lon qilinmagan yoki filiali faol emas.",
        )
    ]


def test_the_list_shape_is_checked(staff_client: APIClient) -> None:
    rows = [product() for _ in range(7)]
    assert errors(put(staff_client, "spotlight", [str(row.pk) for row in rows])) == [
        ("ids", "max_items", "Ko‘pi bilan 6 ta.")
    ]
    assert errors(put(staff_client, "new", [str(rows[0].pk), str(rows[0].pk)])) == [
        ("ids", "invalid", "Har bir element bir marta tanlansin.")
    ]
    assert errors(put(staff_client, "new", ["999999"])) == [("ids", "does_not_exist", "Topilmadi.")]
    assert errors(put(staff_client, "new", ["abc"])) == [("ids", "does_not_exist", "Topilmadi.")]
    assert errors(put(staff_client, "categories", ["no-such-category"])) == [
        ("ids", "does_not_exist", "Topilmadi.")
    ]
    assert errors(put(staff_client, "specialists", [str(row.pk) for row in rows] * 2)) == [
        ("ids", "max_items", "Ko‘pi bilan 8 ta.")
    ]
    response = staff_client.put(f"{LIST}new/", {"ids": "x"}, format="json")
    assert response.status_code == 400
    assert put(staff_client, "banners", []).status_code == 404


def test_a_saved_pick_that_cannot_show_now_is_flagged(staff_client: APIClient) -> None:
    first, second = product(promo=True), product(promo=True)
    assert put(staff_client, "promo", [str(first.pk), str(second.pk)]).status_code == 200
    Product.objects.filter(pk=first.pk).update(promo=False)
    Product.objects.filter(pk=second.pk).update(status=enums.ProductStatus.HIDDEN)
    picks = section(staff_client, "promo")["picks"]
    assert [(item["id"], item["issue"]) for item in picks] == [
        (str(first.pk), "not_promo"),
        (str(second.pk), "hidden"),
    ]


def test_only_staff(anon_client: APIClient) -> None:
    assert anon_client.get(LIST).status_code == 401
    assert put(anon_client, "new", []).status_code == 401
