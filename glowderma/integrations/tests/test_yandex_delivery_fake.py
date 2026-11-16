"""FakeYandexDelivery: deterministic quotes, idempotent creates per request_id, every scenario key, the status
script, cancellation and return rules, and nothing invented under the production settings."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import replace
from decimal import Decimal

import pytest
from django.test import override_settings

from glowderma.integrations.yandex_delivery import fake
from glowderma.integrations.yandex_delivery.fake import (
    DEFAULT_SCRIPT,
    FakeYandexDelivery,
    advance,
    claim_id_for,
)
from glowderma.integrations.yandex_delivery.ports import (
    CancelTerms,
    ClaimRequest,
    Contact,
    ParcelItem,
    Point,
    RoutePoint,
    YandexConflict,
    YandexRejected,
    YandexUnavailable,
)

WAREHOUSE = Point(lon=69.279737, lat=41.311151, fullname="Toshkent, Amir Temur ko'chasi, 1")
CUSTOMER = Point(lon=69.203522, lat=41.285446, fullname="Toshkent, Chilonzor 9, 12-uy")
ITEM = ParcelItem(title="Buyurtma 10248", weight_kg=Decimal("1.2"), cost_value=540_000)
REQUEST = ClaimRequest(
    request_id="3f2b8c1e-6d4a-4f7b-9a0e-1c2d3e4f5a6b",
    external_order_id="10248",
    source=RoutePoint(WAREHOUSE, Contact("Glow Derma", "+998712000000", "sklad@glowderma.uz")),
    destination=RoutePoint(CUSTOMER, Contact("Malika Karimova", "+998901234567")),
    item=ITEM,
    taxi_class="courier",
)


@pytest.fixture(autouse=True)
def clean_fake() -> Iterator[None]:
    fake.reset()
    yield
    fake.reset()


def accepted(scenario: dict[str, object] | None = None) -> tuple[FakeYandexDelivery, str]:
    adapter = FakeYandexDelivery(scenario)
    claim = adapter.create_claim(REQUEST)
    adapter.accept(claim.claim_id, claim.version)
    return adapter, claim.claim_id


def test_quotes_are_deterministic_and_follow_the_scenario() -> None:
    adapter = FakeYandexDelivery()
    courier = adapter.check_price(WAREHOUSE, CUSTOMER, ITEM, "courier")
    assert courier == adapter.check_price(WAREHOUSE, CUSTOMER, ITEM, "courier")
    assert courier.price == fake.price_for(WAREHOUSE, CUSTOMER, "courier") and courier.price % 100 == 0
    assert courier.currency == "UZS" and courier.zone_id == "tashkent" and courier.distance_m
    express = adapter.check_price(WAREHOUSE, CUSTOMER, ITEM, "express")
    assert express.price > courier.price
    assert (
        FakeYandexDelivery({"price": 31_000}).check_price(WAREHOUSE, CUSTOMER, ITEM, "courier").price
        == 31_000
    )
    for answer, error, code in (
        ("rejected", YandexRejected, "errors.suitable_offer_not_found"),
        ("no_tariff", YandexRejected, "estimating.tariff.no_categories_in_zone"),
        ("down", YandexUnavailable, None),
    ):
        with pytest.raises(error) as failure:
            FakeYandexDelivery({"check_price": answer}).check_price(WAREHOUSE, CUSTOMER, ITEM, "courier")
        if code:
            assert isinstance(failure.value, YandexRejected) and failure.value.code == code
    assert [tariff.name for tariff in adapter.tariffs(WAREHOUSE)] == ["courier", "express"]
    assert [tariff.name for tariff in FakeYandexDelivery({"tariffs": ["express"]}).tariffs(WAREHOUSE)] == [
        "express"
    ]


def test_create_is_idempotent_per_request_id_whatever_the_body() -> None:
    adapter = FakeYandexDelivery({"offer_price": 27_000, "warnings": ["not_fit_in_car"]})
    first = adapter.create_claim(REQUEST)
    assert first.claim_id == claim_id_for(REQUEST.request_id) and len(first.claim_id) == 32
    assert (first.status, first.version, first.offer_price) == ("ready_for_approval", 1, 27_000)
    assert first.warnings == ("not_fit_in_car",) and first.offer_valid_until is not None
    assert first.external_order_id == "10248" and first.destination_point_id == 2
    replay = FakeYandexDelivery({"create": "rejected"}).create_claim(replace(REQUEST, taxi_class="express"))
    assert replay == first  # a replay returns the old claim regardless of the body (FAQ 1)
    other = adapter.create_claim(replace(REQUEST, request_id="another-uuid"))
    assert other.claim_id != first.claim_id


def test_a_create_that_timed_out_after_creating_is_found_by_the_retry() -> None:
    with pytest.raises(YandexUnavailable) as unknown:
        FakeYandexDelivery({"create": "down_after_create"}).create_claim(REQUEST)
    assert unknown.value.outcome_unknown is True
    retry = FakeYandexDelivery({"create": "down_after_create"}).create_claim(REQUEST)
    assert retry.claim_id == claim_id_for(REQUEST.request_id)
    assert len(fake._claims) == 1


def test_refused_or_failed_creates_store_nothing() -> None:
    with pytest.raises(YandexRejected) as refused:
        FakeYandexDelivery({"create": "rejected"}).create_claim(REQUEST)
    assert refused.value.code == "address_not_found"
    with pytest.raises(YandexUnavailable) as down:
        FakeYandexDelivery({"create": "down"}).create_claim(REQUEST)
    assert down.value.outcome_unknown is False
    assert fake._claims == {}
    bad = replace(REQUEST, destination=RoutePoint(CUSTOMER, Contact("X", "+7 912 345 67 89")))
    with pytest.raises(YandexRejected) as invalid:
        FakeYandexDelivery().create_claim(bad)
    assert invalid.value.code == "invalid_request" and fake._claims == {}


def test_estimate_scenarios() -> None:
    failed = FakeYandexDelivery({"estimate": "failed"}).create_claim(REQUEST)
    assert failed.status == "estimating_failed" and failed.offer_price is None
    assert failed.error_codes == ("estimating.cant_construct_route",)
    pending = FakeYandexDelivery({"estimate": "pending"}).create_claim(replace(REQUEST, request_id="r-2"))
    adapter = FakeYandexDelivery()
    assert adapter.claim(pending.claim_id).status == "estimating"  # reads never finish an estimate
    assert advance(pending.claim_id, "ready_for_approval").status == "ready_for_approval"


def test_the_default_script_runs_one_status_per_read_after_accept() -> None:
    adapter = FakeYandexDelivery({"final_price": 26_500})
    claim = adapter.create_claim(REQUEST)
    assert adapter.claim(claim.claim_id).status == "ready_for_approval"  # nothing moves before accept
    assert adapter.tracking_link(claim.claim_id) is None
    answer = adapter.accept(claim.claim_id, 1)
    assert answer is not None and answer.status == "accepted" and answer.offer_price is None
    seen = [adapter.claim(claim.claim_id) for _ in DEFAULT_SCRIPT]
    assert [snapshot.status for snapshot in seen] == list(DEFAULT_SCRIPT)
    assert seen[0].courier_name == "" and seen[1].courier_name == fake.COURIER_NAME
    assert seen[1].courier_vehicle == fake.COURIER_VEHICLE
    assert seen[-1].final_price == 26_500 and seen[-2].final_price is None
    assert adapter.claim(claim.claim_id).status == "delivered_finish"  # the last status stays
    assert adapter.tracking_link(claim.claim_id) is None  # finished


def test_custom_statuses_bulk_reads_and_tracking() -> None:
    adapter, claim_id = accepted({"statuses": ["performer_found", "performer_not_found"]})
    other = adapter.create_claim(replace(REQUEST, request_id="r-2")).claim_id
    first = adapter.claims([claim_id, other, "unknown", claim_id])
    assert [(snapshot.claim_id, snapshot.status) for snapshot in first] == [
        (claim_id, "performer_found"),
        (other, "ready_for_approval"),
    ]
    assert adapter.tracking_link(claim_id) == fake.TRACKING_BASE + claim_id
    assert adapter.claims([claim_id])[0].status == "performer_not_found"
    assert adapter.claim(claim_id).final_price is None  # no delivery, no price
    with pytest.raises(YandexRejected) as unknown:
        adapter.claim("unknown")
    assert unknown.value.code == "not_found"


def test_accept_scenarios() -> None:
    adapter = FakeYandexDelivery({"accept": "conflict"})
    claim = adapter.create_claim(REQUEST)
    with pytest.raises(YandexConflict) as conflict:
        adapter.accept(claim.claim_id, 1)
    assert conflict.value.code == "inappropriate_status"
    with pytest.raises(YandexUnavailable) as down:
        FakeYandexDelivery({"accept": "down"}).accept(claim.claim_id, 1)
    assert down.value.outcome_unknown is False
    plain = FakeYandexDelivery()
    with pytest.raises(YandexConflict) as old:
        plain.accept(claim.claim_id, 7)
    assert old.value.code == "old_version"
    expired = FakeYandexDelivery({"accept": "expired"})
    assert expired.accept(claim.claim_id, 1) is not None
    snapshot = plain.claim(claim.claim_id)
    assert snapshot.status == "failed" and snapshot.error_codes == ("offer_expired",)
    with pytest.raises(YandexConflict):  # never accepted twice
        plain.accept(claim.claim_id, 1)


def test_accept_scenarios_for_the_first_accept_only_and_lost_answers() -> None:
    adapter = FakeYandexDelivery({"accept": "down_once"})
    claim = adapter.create_claim(REQUEST)
    assert claim.item_extra_ids == ("10248",)  # the order reference is echoed like Yandex does
    with pytest.raises(YandexUnavailable) as down:
        adapter.accept(claim.claim_id, 1)
    assert down.value.outcome_unknown is False
    assert adapter.claim(claim.claim_id).status == "ready_for_approval"
    assert adapter.accept(claim.claim_id, 1) is not None  # the second one goes through
    assert adapter.claim(claim.claim_id).status == "performer_lookup"
    edited = FakeYandexDelivery({"accept": "old_version_once"})
    other = edited.create_claim(replace(REQUEST, request_id="r-old"))
    with pytest.raises(YandexConflict) as old:
        edited.accept(other.claim_id, 1)
    assert old.value.code == "old_version"
    assert edited.claim(other.claim_id).version == 2  # the fresh version shows on the next read
    with pytest.raises(YandexConflict):
        edited.accept(other.claim_id, 1)  # the stale version is still refused
    assert edited.accept(other.claim_id, 2) is not None
    lost = FakeYandexDelivery({"accept": "timeout"})
    third = lost.create_claim(replace(REQUEST, request_id="r-lost"))
    with pytest.raises(YandexUnavailable) as timeout:
        lost.accept(third.claim_id, 1)
    assert timeout.value.outcome_unknown is True
    assert lost.claim(third.claim_id).status == "ready_for_approval"  # nothing was accepted
    unknown = FakeYandexDelivery({"info": "not_found"})
    with pytest.raises(YandexRejected) as missing:
        unknown.claim(third.claim_id)
    assert missing.value.code == "not_found"
    assert [snapshot.claim_id for snapshot in unknown.claims([third.claim_id])] == [third.claim_id]


def test_cancel_terms_follow_the_courier() -> None:
    adapter = FakeYandexDelivery()
    claim = adapter.create_claim(REQUEST)
    assert adapter.cancel_terms(claim.claim_id) == CancelTerms(state="free")
    advance(claim.claim_id, "pickup_arrived")
    assert adapter.cancel_terms(claim.claim_id) == CancelTerms(state="paid", price=claim.offer_price)
    with pytest.raises(YandexConflict) as conflict:
        adapter.cancel(claim.claim_id, 1, "free")
    assert conflict.value.code == "free_cancel_is_unavailable"
    adapter.cancel(claim.claim_id, 1, "paid")
    cancelled = adapter.claim(claim.claim_id)
    assert cancelled.status == "cancelled_with_payment" and cancelled.final_price == claim.offer_price
    assert adapter.cancel_terms(claim.claim_id).state == "unavailable"
    with pytest.raises(YandexConflict):
        adapter.cancel(claim.claim_id, 1, "paid")


def test_cancel_free_and_forced_terms() -> None:
    adapter, claim_id = accepted()
    adapter.claim(claim_id)  # performer_lookup
    with pytest.raises(YandexConflict) as old:
        adapter.cancel(claim_id, 9, "free")
    assert old.value.code == "old_version"
    adapter.cancel(claim_id, 1, "free")
    assert adapter.claim(claim_id).status == "cancelled"  # the script stops
    other = adapter.create_claim(replace(REQUEST, request_id="r-2")).claim_id
    with pytest.raises(YandexConflict):
        FakeYandexDelivery({"cancel": "unavailable"}).cancel(other, 1, "free")
    with pytest.raises(YandexUnavailable):
        FakeYandexDelivery({"cancel": "down"}).cancel_terms(other)
    with pytest.raises(YandexUnavailable):
        FakeYandexDelivery({"cancel": "down"}).cancel(other, 1, "free")
    assert FakeYandexDelivery({"cancel": "paid"}).cancel_terms(other).state == "paid"


def test_return_only_after_pickup() -> None:
    adapter, claim_id = accepted()
    with pytest.raises(YandexConflict) as early:
        adapter.request_return(claim_id, 2, "Mijoz rad etdi")
    assert early.value.code == "state_mismatch"
    advance(claim_id, "pickuped")
    with pytest.raises(YandexRejected):
        adapter.request_return(claim_id, 1, "wrong point")
    with pytest.raises(YandexConflict) as refused:
        FakeYandexDelivery({"return": "refused"}).request_return(claim_id, 2, "x")
    assert refused.value.code == "not_allowed"
    adapter.request_return(claim_id, 2, "Mijoz rad etdi")
    statuses = [adapter.claim(claim_id).status for _ in range(4)]
    assert statuses == ["return_arrived", "returned", "returned_finish", "returned_finish"]
    assert adapter.claim(claim_id).final_price == adapter.claim(claim_id).offer_price


def test_advance_takes_over_the_script() -> None:
    adapter, claim_id = accepted()
    assert advance(claim_id, "delivery_arrived").status == "delivery_arrived"
    assert adapter.claim(claim_id).status == "delivery_arrived"
    with pytest.raises(KeyError):
        advance("unknown", "delivered")


@override_settings(DEV_FEATURES_ALLOWED=False, YANDEX_DELIVERY_MODE="fake")
def test_under_the_production_settings_nothing_is_invented() -> None:
    adapter = FakeYandexDelivery({"price": 1, "create": "down_after_create"})
    assert adapter.scenario == {}
    for call in (
        lambda: adapter.check_price(WAREHOUSE, CUSTOMER, ITEM, "courier"),
        lambda: adapter.create_claim(REQUEST),
    ):
        with pytest.raises(YandexRejected) as refused:
            call()
        assert refused.value.code == "fake_mode"
    for read in (
        lambda: adapter.tariffs(WAREHOUSE),
        lambda: adapter.claim("x"),
        lambda: adapter.claims(["x"]),
        lambda: adapter.accept("x", 1),
        lambda: adapter.cancel_terms("x"),
        lambda: adapter.cancel("x", 1, "free"),
        lambda: adapter.request_return("x", 2, "x"),
        lambda: adapter.tracking_link("x"),
    ):
        with pytest.raises(YandexUnavailable):
            read()
    assert fake._claims == {}
