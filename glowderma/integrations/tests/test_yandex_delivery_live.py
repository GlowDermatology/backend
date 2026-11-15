"""The live Yandex Delivery adapter (plan §3.2) against answers shaped like the documented examples: payloads,
query ids, headers, money rounding, the currency guard and every failure mapping — never the network."""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import httpx
import pytest
from django.test import override_settings

from glowderma.integrations import httpclient
from glowderma.integrations.yandex_delivery import protocol
from glowderma.integrations.yandex_delivery.fake import FakeYandexDelivery
from glowderma.integrations.yandex_delivery.live import LiveYandexDelivery
from glowderma.integrations.yandex_delivery.ports import (
    CancelTerms,
    Claim,
    ClaimRequest,
    Contact,
    ParcelItem,
    Point,
    RoutePoint,
    Tariff,
    YandexConflict,
    YandexRejected,
    YandexUnavailable,
)
from glowderma.integrations.yandex_delivery.registry import get_yandex_delivery

from .live_fixtures import Script, connect_error, load, read_timeout, reply

BASE = "https://b2b.taxi.yandex.net"
TOKEN = "y0_AgAAAAB-secret-oauth-token-value"  # noqa: S105 - test value
CLAIM_ID = "7f5c2a9b1d3e4f60a1b2c3d4e5f60718"
REQUEST_ID = "3f2b8c1e-6d4a-4f7b-9a0e-1c2d3e4f5a6b"
WAREHOUSE = Point(lon=69.279737, lat=41.311151, fullname="Toshkent, Amir Temur ko'chasi, 1")
CUSTOMER = Point(lon=69.2035224, lat=41.2854461, fullname="Toshkent, Chilonzor 9, 12-uy")
ITEM = ParcelItem(
    title="Buyurtma 10248",
    quantity=1,
    weight_kg=Decimal("1.2"),
    size_m=(Decimal("0.3"), Decimal("0.2"), Decimal("0.1")),
    cost_value=540_000,
)
REQUEST = ClaimRequest(
    request_id=REQUEST_ID,
    external_order_id="10248",
    source=RoutePoint(
        WAREHOUSE, Contact("Glow Derma ombori", "+998 71 200-00-00", "sklad@glowderma.uz"), "2-kirish"
    ),
    destination=RoutePoint(CUSTOMER, Contact("Malika Karimova", "998901234567"), "3-podyezd"),
    item=ITEM,
    taxi_class="courier",
    skip_confirmation_destination=True,
    callback_url="https://api.glowderma.uz/api/v1/webhooks/yandex-delivery/?ref=10248-abc&",
)
INFO = protocol.PATH_INFO


@pytest.fixture(autouse=True)
def fast_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(httpclient, "RETRY_BACKOFF_SECONDS", 0)


def yandex(script: Script, **overrides: Any) -> LiveYandexDelivery:
    values: dict[str, Any] = {"base_url": BASE + "/", "token": TOKEN, "currency": "UZS", "language": "ru"}
    return LiveYandexDelivery(**{**values, **overrides}, transport=script.transport)


def body(request: httpx.Request) -> Any:
    return json.loads(request.content) if request.content else None


def claim_json(name: str, **changes: Any) -> httpx.Response:
    document = load("yandex", name)
    document.update(changes)
    return httpx.Response(200, json=document)


# --- headers, ids and secrets ---------------------------------------------------------------------------


def test_every_request_carries_the_bearer_token_and_the_language_never_in_the_url() -> None:
    script = (
        Script()
        .on("POST", protocol.PATH_TARIFFS, reply("yandex", "tariffs_ok"))
        .on("POST", INFO, reply("yandex", "claim_ready"))
        .on("GET", protocol.PATH_TRACKING_LINKS, reply("yandex", "tracking_links_ok"))
    )
    adapter = yandex(script, language="en")
    adapter.tariffs(WAREHOUSE)
    adapter.claim(CLAIM_ID)
    adapter.tracking_link(CLAIM_ID)
    assert len(script.requests) == 3
    for request in script.requests:
        assert request.headers["Authorization"] == f"Bearer {TOKEN}"
        assert request.headers["Accept-Language"] == "en"
        assert request.headers["Accept"] == "application/json"
        assert TOKEN not in str(request.url)
        assert str(request.url).startswith(BASE + protocol.PREFIX + "/")
    info = script.to("POST", INFO)[0]
    assert dict(info.url.params) == {"claim_id": CLAIM_ID} and info.content == b""


def test_the_token_never_reaches_a_log_line_or_an_error(caplog: pytest.LogCaptureFixture) -> None:
    script = (
        Script()
        .on("POST", INFO, reply("yandex", "error_401", 401), reply("yandex", "error_500", 500), read_timeout)
        .on("POST", protocol.PATH_ACCEPT, reply("yandex", "accept_ok"))
    )
    adapter = yandex(script)
    errors: list[str] = []
    with caplog.at_level(logging.DEBUG):
        for _ in range(3):
            with pytest.raises(YandexUnavailable) as failure:
                adapter.claim(CLAIM_ID)
            errors.append(str(failure.value))
        adapter.accept(CLAIM_ID, 1)
    assert TOKEN not in caplog.text and not any(TOKEN in error for error in errors)
    assert TOKEN not in repr(adapter)
    assert "yandex.auth_failed status=401" in caplog.text
    # log lines carry the path and the status, never the query (claim id) or a body
    assert f"yandex POST {INFO} -> 401" in caplog.text
    assert CLAIM_ID not in caplog.text


def test_create_passes_request_id_as_a_query_and_sends_the_documented_body() -> None:
    script = Script().on("POST", protocol.PATH_CREATE, reply("yandex", "claim_new"))
    claim = yandex(script).create_claim(REQUEST)
    (request,) = script.requests
    assert dict(request.url.params) == {"request_id": REQUEST_ID}
    assert body(request) == {
        "items": [
            {
                "title": "Buyurtma 10248",
                "cost_value": "540000",
                "cost_currency": "UZS",
                "quantity": 1,
                "pickup_point": 1,
                "droppof_point": 2,
                "extra_id": "10248",
                "weight": 1.2,
                "size": {"length": 0.3, "width": 0.2, "height": 0.1},
            }
        ],
        "route_points": [
            {
                "point_id": 1,
                "visit_order": 1,
                "type": "source",
                "contact": {
                    "name": "Glow Derma ombori",
                    "phone": "+998712000000",
                    "email": "sklad@glowderma.uz",
                },
                "address": {
                    "fullname": "Toshkent, Amir Temur ko'chasi, 1",
                    "coordinates": [69.279737, 41.311151],
                    "comment": "2-kirish",
                },
                "skip_confirmation": False,
            },
            {
                "point_id": 2,
                "visit_order": 2,
                "type": "destination",
                "contact": {"name": "Malika Karimova", "phone": "+998901234567"},
                "address": {
                    "fullname": "Toshkent, Chilonzor 9, 12-uy",
                    "coordinates": [69.203522, 41.285446],  # [lon, lat], six decimals
                    "comment": "3-podyezd",
                },
                "skip_confirmation": True,
                "external_order_id": "10248",
            },
        ],
        "client_requirements": {"taxi_class": "courier"},
        "optional_return": False,
        "callback_properties": {
            "callback_url": "https://api.glowderma.uz/api/v1/webhooks/yandex-delivery/?ref=10248-abc&"
        },
    }
    assert claim == Claim(
        claim_id=CLAIM_ID,
        status="new",
        version=1,
        updated_ts=datetime(2026, 11, 15, 10, 0, tzinfo=UTC),
        destination_point_id=6988,
        external_order_id="10248",
        taxi_class="courier",
    )


def test_data_we_cannot_send_is_refused_before_any_request() -> None:
    script = Script()
    adapter = yandex(script)
    bad_phone = RoutePoint(CUSTOMER, Contact("Malika", "+7 912 345 67 89"))
    far_away = Point(lon=200.0, lat=41.0, fullname="x")
    cases: list[Callable[[], object]] = [
        lambda: adapter.create_claim(replace(REQUEST, destination=bad_phone)),
        lambda: adapter.create_claim(replace(REQUEST, taxi_class="bicycle")),
        lambda: adapter.create_claim(replace(REQUEST, callback_url="https://x.uz/cb")),
        lambda: adapter.check_price(WAREHOUSE, far_away, ITEM, "courier"),
        lambda: adapter.claim("not a claim id!"),
        lambda: adapter.accept("../../admin", 1),
    ]
    for case in cases:
        with pytest.raises(YandexRejected) as refused:
            case()
        assert refused.value.code in {"invalid_request", "invalid_claim_id"}
    assert script.requests == []


# --- quotes ---------------------------------------------------------------------------------------------


def test_tariffs_reads_the_classes_of_the_point() -> None:
    script = Script().on("POST", protocol.PATH_TARIFFS, reply("yandex", "tariffs_ok"))
    tariffs = yandex(script).tariffs(WAREHOUSE)
    assert body(script.requests[0]) == {
        "start_point": [69.279737, 41.311151],
        "fullname": "Toshkent, Amir Temur ko'chasi, 1",
    }
    assert tariffs == [
        Tariff(name="courier", minimal_price=15_000, title="Курьер"),  # 14999.5 rounded up
        Tariff(name="express", minimal_price=19_000, title="Экспресс"),
    ]


def test_check_price_sends_both_points_and_reads_the_estimate() -> None:
    script = Script().on("POST", protocol.PATH_CHECK_PRICE, reply("yandex", "check_price_ok"))
    estimate = yandex(script).check_price(WAREHOUSE, CUSTOMER, ITEM, "express")
    assert body(script.requests[0]) == {
        "route_points": [
            {"id": 1, "coordinates": [69.279737, 41.311151], "fullname": "Toshkent, Amir Temur ko'chasi, 1"},
            {"id": 2, "coordinates": [69.203522, 41.285446], "fullname": "Toshkent, Chilonzor 9, 12-uy"},
        ],
        "items": [
            {
                "quantity": 1,
                "pickup_point": 1,
                "dropoff_point": 2,
                "weight": 1.2,
                "size": {"length": 0.3, "width": 0.2, "height": 0.1},
            }
        ],
        "requirements": {"taxi_class": "express"},
    }
    assert (estimate.price, estimate.currency, estimate.raw_price) == (24_501, "UZS", "24500.5000")
    assert (estimate.eta_minutes, estimate.distance_m, estimate.zone_id) == (25, 7242, "tashkent")


def test_check_price_refusals_and_foreign_currencies() -> None:
    for name, status, code in (
        ("check_price_409", 409, "estimating.tariff.no_categories_in_zone"),
        ("check_price_400", 400, "address_not_found"),
    ):
        script = Script().on("POST", protocol.PATH_CHECK_PRICE, reply("yandex", name, status))
        with pytest.raises(YandexRejected) as refused:  # a 409 of check-price is no claim conflict
            yandex(script).check_price(WAREHOUSE, CUSTOMER, ITEM, "courier")
        assert refused.value.code == code
    ok = load("yandex", "check_price_ok")
    rub = {**ok, "currency_rules": {**ok["currency_rules"], "code": "RUB"}}
    no_currency = {key: value for key, value in ok.items() if key != "currency_rules"}
    for document in (rub, no_currency, {**ok, "price": "-1"}, {**ok, "price": "12,5"}):
        script = Script().on("POST", protocol.PATH_CHECK_PRICE, httpx.Response(200, json=document))
        with pytest.raises(YandexUnavailable):
            yandex(script).check_price(WAREHOUSE, CUSTOMER, ITEM, "courier")


@pytest.mark.parametrize(
    ("raw", "so_m"),
    [
        ("0", 0),
        ("100", 100),
        ("100.0001", 101),
        ("24500.5000", 24_501),
        ("99.9", 100),
        ("99.", 99),
        (12.5, 13),
        (7, 7),
    ],
)
def test_money_is_rounded_up_to_whole_so_m(raw: object, so_m: int) -> None:
    assert protocol.so_m(raw) == so_m


@pytest.mark.parametrize("raw", ["-1", "1e5", "abc", "", "100000000.0001", True, None, float("nan"), [1]])
def test_unusable_amounts_are_shape_errors(raw: object) -> None:
    with pytest.raises(protocol.ShapeError):
        protocol.so_m(raw)


# --- claims ---------------------------------------------------------------------------------------------


def test_claim_info_prefers_the_vat_inclusive_offer() -> None:
    script = Script().on("POST", INFO, reply("yandex", "claim_ready"))
    claim = yandex(script).claim(CLAIM_ID)
    assert claim.status == "ready_for_approval" and claim.version == 1
    assert claim.offer_price == 25_201  # price_with_vat 25200.4000, rounded up
    assert claim.offer_valid_until == datetime(2026, 11, 15, 10, 10, 4, tzinfo=UTC)
    assert claim.updated_ts is not None and claim.updated_ts.tzinfo is not None
    assert claim.currency == "UZS" and claim.warnings == ("address_too_far",)
    assert claim.final_price is None and claim.courier_name == "" and claim.taxi_class == "courier"
    document = load("yandex", "claim_ready")
    document["pricing"]["offer"].pop("price_with_vat")
    script = Script().on("POST", INFO, httpx.Response(200, json=document))
    assert yandex(script).claim(CLAIM_ID).offer_price == 21_000  # without VAT when nothing else is sent


def test_claim_courier_final_price_and_return_reasons_without_any_phone() -> None:
    script = Script().on(
        "POST",
        INFO,
        reply("yandex", "claim_performer_found"),
        reply("yandex", "claim_returning"),
        reply("yandex", "claim_delivered_finish"),
    )
    adapter = yandex(script)
    found = adapter.claim(CLAIM_ID)
    assert (found.courier_name, found.courier_vehicle) == ("Rustam", "Chevrolet Cobalt oq 01A123BC")
    assert "+998" not in repr(found)
    returning = adapter.claim(CLAIM_ID)
    assert returning.status == "returning" and returning.return_reasons == ("recipient_unavailable",)
    finished = adapter.claim(CLAIM_ID)
    assert finished.status == "delivered_finish" and finished.final_price == 26_101


def test_unknown_statuses_are_kept_and_unknown_claims_refused() -> None:
    script = Script().on("POST", INFO, claim_json("claim_new", status="pay_waiting"))
    assert yandex(script).claim(CLAIM_ID).status == "pay_waiting"
    script = Script().on("POST", INFO, reply("yandex", "info_404", 404))
    with pytest.raises(YandexRejected) as refused:
        yandex(script).claim(CLAIM_ID)
    assert refused.value.code == "not_found"


def test_a_claim_priced_in_another_currency_is_unavailable(caplog: pytest.LogCaptureFixture) -> None:
    script = Script().on("POST", INFO, reply("yandex", "claim_rub"))
    with caplog.at_level(logging.ERROR), pytest.raises(YandexUnavailable):
        yandex(script).claim(CLAIM_ID)
    assert "yandex.currency_mismatch" in caplog.text
    document = load("yandex", "claim_ready")
    document["pricing"].pop("currency")
    document["pricing"].pop("currency_rules")
    script = Script().on("POST", INFO, httpx.Response(200, json=document))
    with pytest.raises(YandexUnavailable):  # a price whose currency is not said is not a price we keep
        yandex(script).claim(CLAIM_ID)


@pytest.mark.parametrize(
    "responder",
    [
        httpx.Response(200, content=b"not json"),
        httpx.Response(200, json=[]),
        httpx.Response(200, json={}),
        claim_json("claim_new", id=None),
        claim_json("claim_new", version="one"),
        claim_json("claim_new", status=""),
        claim_json("claim_ready", pricing={"offer": {"price": "free"}, "currency": "UZS"}),
        httpx.Response(200, headers={"Content-Encoding": "gzip"}, stream=httpx.ByteStream(b"no gzip")),
    ],
)
def test_unreadable_answers_are_unavailable(responder: httpx.Response) -> None:
    script = Script().on("POST", INFO, responder)
    with pytest.raises(YandexUnavailable) as failure:
        yandex(script).claim(CLAIM_ID)
    assert failure.value.outcome_unknown is True


def test_bulk_info_asks_at_most_a_thousand_ids_per_request() -> None:
    seen: list[list[str]] = []

    def bulk(request: httpx.Request) -> httpx.Response:
        seen.append(body(request)["claim_ids"])
        return reply("yandex", "bulk_info")

    script = Script().on("POST", protocol.PATH_BULK_INFO, bulk)
    ids = [f"{index:032x}" for index in range(1500)]
    claims = yandex(script).claims([*ids, ids[0], "bad id!"])
    assert [len(chunk) for chunk in seen] == [1000, 500] and seen[0][0] == ids[0]
    assert len(claims) == 4 and {claim.status for claim in claims} == {"performer_found", "delivered_finish"}
    assert yandex(Script()).claims([]) == []


def test_bulk_info_skips_unreadable_entries_and_fails_on_an_unreadable_answer(
    caplog: pytest.LogCaptureFixture,
) -> None:
    good = load("yandex", "claim_performer_found")
    rub = load("yandex", "claim_rub")
    entries = [good, {"id": "8a6d3b0c2e4f5a71b2c3d4e5f6071829", "status": "new"}, rub, "garbage"]
    script = Script().on("POST", protocol.PATH_BULK_INFO, httpx.Response(200, json={"claims": entries}))
    with caplog.at_level(logging.WARNING):
        claims = yandex(script).claims([CLAIM_ID, "8a6d3b0c2e4f5a71b2c3d4e5f6071829"])
    assert [claim.claim_id for claim in claims] == [CLAIM_ID]
    assert "yandex.unreadable_claim claim_id=8a6d3b0c2e4f5a71b2c3d4e5f6071829" in caplog.text
    assert "yandex.currency_mismatch" in caplog.text
    script = Script().on("POST", protocol.PATH_BULK_INFO, httpx.Response(200, json={"items": []}))
    with pytest.raises(YandexUnavailable):
        yandex(script).claims([CLAIM_ID])


def test_accept_sends_the_version_once_and_maps_its_outcomes() -> None:
    script = Script().on("POST", protocol.PATH_ACCEPT, reply("yandex", "accept_ok"))
    accepted = yandex(script).accept(CLAIM_ID, 3)
    (request,) = script.requests
    assert dict(request.url.params) == {"claim_id": CLAIM_ID} and body(request) == {"version": 3}
    assert accepted == Claim(claim_id=CLAIM_ID, status="new", version=1)  # the answer's status proves nothing
    script = Script().on("POST", protocol.PATH_ACCEPT, httpx.Response(200, content=b""))
    assert yandex(script).accept(CLAIM_ID, 1) is None  # processed, body unreadable: re-read the claim
    script = Script().on("POST", protocol.PATH_ACCEPT, reply("yandex", "accept_409", 409))
    with pytest.raises(YandexConflict) as conflict:
        yandex(script).accept(CLAIM_ID, 1)
    assert conflict.value.code == "offer_expired"
    script = Script().on("POST", protocol.PATH_ACCEPT, reply("yandex", "info_404", 404))
    with pytest.raises(YandexRejected):
        yandex(script).accept(CLAIM_ID, 1)
    script = Script().on("POST", protocol.PATH_ACCEPT, read_timeout)
    with pytest.raises(YandexUnavailable) as unknown:
        yandex(script).accept(CLAIM_ID, 1)
    assert unknown.value.outcome_unknown is True and len(script.requests) == 1  # never resent


def test_a_create_that_may_have_left_is_never_resent_by_the_adapter() -> None:
    for responder in (read_timeout, reply("yandex", "error_500", 500)):
        script = Script().on("POST", protocol.PATH_CREATE, responder)
        with pytest.raises(YandexUnavailable) as unknown:
            yandex(script).create_claim(REQUEST)
        assert unknown.value.outcome_unknown is True and len(script.requests) == 1
    script = Script().on("POST", protocol.PATH_CREATE, connect_error)
    with pytest.raises(YandexUnavailable) as unsent:
        yandex(script).create_claim(REQUEST)
    assert unsent.value.outcome_unknown is False  # never left: httpclient retried within the deadline
    script = Script().on("POST", protocol.PATH_CREATE, reply("yandex", "create_400", 400))
    with pytest.raises(YandexRejected) as refused:
        yandex(script).create_claim(REQUEST)
    assert refused.value.code == "country_phone_code_not_supported"
    assert "123 45 67" not in refused.value.message and "***" in refused.value.message  # safe_text


def test_a_spent_deadline_sends_nothing() -> None:
    script = Script().on("POST", protocol.PATH_CHECK_PRICE, reply("yandex", "check_price_ok"))
    with pytest.raises(YandexUnavailable) as failure:
        yandex(script).check_price(WAREHOUSE, CUSTOMER, ITEM, "courier", deadline=httpclient.Deadline(0))
    assert failure.value.outcome_unknown is False and script.requests == []


@pytest.mark.parametrize(
    ("status", "error", "outcome_unknown"),
    [
        (301, YandexUnavailable, False),
        (401, YandexUnavailable, False),
        (403, YandexUnavailable, False),
        (408, YandexUnavailable, False),
        (425, YandexUnavailable, False),
        (429, YandexUnavailable, False),
        (500, YandexUnavailable, True),
        (502, YandexUnavailable, True),
        (503, YandexUnavailable, True),
        (400, YandexRejected, None),
        (404, YandexRejected, None),
        (410, YandexRejected, None),
        (422, YandexRejected, None),
        (409, YandexConflict, None),
    ],
)
def test_error_mapping_of_a_claim_write(
    status: int, error: type[Exception], outcome_unknown: bool | None
) -> None:
    script = Script().on("POST", protocol.PATH_CANCEL, httpx.Response(status, json={"code": "x_code"}))
    with pytest.raises(error) as failure:
        yandex(script).cancel(CLAIM_ID, 1, "free")
    if outcome_unknown is not None:
        assert isinstance(failure.value, YandexUnavailable)
        assert failure.value.outcome_unknown is outcome_unknown
    else:
        assert isinstance(failure.value, YandexRejected | YandexConflict) and failure.value.code == "x_code"
    assert len(script.requests) == 1


def test_an_error_without_a_code_names_the_status() -> None:
    script = Script().on("POST", INFO, httpx.Response(400, content=b"<html>bad</html>"))
    with pytest.raises(YandexRejected) as refused:
        yandex(script).claim(CLAIM_ID)
    assert refused.value.code == "http_400"


# --- cancellation, return, tracking ---------------------------------------------------------------------


def test_cancel_terms() -> None:
    script = Script().on(
        "POST",
        protocol.PATH_CANCEL_INFO,
        reply("yandex", "cancel_info_free"),
        reply("yandex", "cancel_info_paid"),
    )
    adapter = yandex(script)
    assert adapter.cancel_terms(CLAIM_ID) == CancelTerms(state="free", price=None)
    assert adapter.cancel_terms(CLAIM_ID) == CancelTerms(state="paid", price=12_001)
    assert dict(script.requests[0].url.params) == {"claim_id": CLAIM_ID}
    paid = load("yandex", "cancel_info_paid")
    cases: list[tuple[dict[str, Any], CancelTerms | None]] = [
        (
            {**paid, "currency": None},
            CancelTerms(state="paid", price=None),
        ),  # no currency: the fee is dropped
        ({**paid, "currency": "RUB"}, None),
        ({"cancel_state": "maybe"}, None),
    ]
    for document, expected in cases:
        script = Script().on("POST", protocol.PATH_CANCEL_INFO, httpx.Response(200, json=document))
        if expected is None:
            with pytest.raises(YandexUnavailable):
                yandex(script).cancel_terms(CLAIM_ID)
        else:
            assert yandex(script).cancel_terms(CLAIM_ID) == expected


def test_cancel_sends_version_and_terms() -> None:
    script = Script().on("POST", protocol.PATH_CANCEL, reply("yandex", "cancel_ok"))
    yandex(script).cancel(CLAIM_ID, 2, "paid")
    (request,) = script.requests
    assert dict(request.url.params) == {"claim_id": CLAIM_ID}
    assert body(request) == {"version": 2, "cancel_state": "paid"}
    script = Script().on("POST", protocol.PATH_CANCEL, reply("yandex", "cancel_409", 409))
    with pytest.raises(YandexConflict) as conflict:
        yandex(script).cancel(CLAIM_ID, 1, "free")
    assert conflict.value.code == "free_cancel_is_unavailable"
    with pytest.raises(ValueError, match="cancel_state"):
        yandex(Script()).cancel(CLAIM_ID, 1, "unavailable")


def test_request_return() -> None:
    script = Script().on("POST", protocol.PATH_RETURN, httpx.Response(200))
    yandex(script).request_return(CLAIM_ID, 6988, "Mijoz buyurtmani rad etdi")
    (request,) = script.requests
    assert dict(request.url.params) == {"claim_id": CLAIM_ID}
    assert body(request) == {"point_id": 6988, "comment": "Mijoz buyurtmani rad etdi"}
    script = Script().on("POST", protocol.PATH_RETURN, reply("yandex", "return_409", 409))
    with pytest.raises(YandexConflict) as conflict:
        yandex(script).request_return(CLAIM_ID, 6988, "x")
    assert conflict.value.code == "state_mismatch"
    script = Script().on("POST", protocol.PATH_RETURN, reply("yandex", "return_410", 410))
    with pytest.raises(YandexRejected) as gone:
        yandex(script).request_return(CLAIM_ID, 6988, "x")
    assert gone.value.code == "inappropriate_status"


def test_tracking_link_is_the_destinations_https_link() -> None:
    script = Script().on(
        "GET",
        protocol.PATH_TRACKING_LINKS,
        reply("yandex", "tracking_links_ok"),
        reply("yandex", "tracking_links_409", 409),
    )
    adapter = yandex(script)
    assert adapter.tracking_link(CLAIM_ID) == f"https://dostavka.yandex.ru/route/{CLAIM_ID}"
    assert adapter.tracking_link(CLAIM_ID) is None  # no courier link yet
    assert dict(script.requests[0].url.params) == {"claim_id": CLAIM_ID}
    plain = load("yandex", "tracking_links_ok")
    plain["route_points"][1]["sharing_link"] = "http://dostavka.yandex.ru/route/x"
    script = Script().on("GET", protocol.PATH_TRACKING_LINKS, httpx.Response(200, json=plain))
    assert yandex(script).tracking_link(CLAIM_ID) is None


# --- configuration --------------------------------------------------------------------------------------


def test_is_configured_needs_a_token_https_a_currency_and_a_language() -> None:
    assert yandex(Script()).is_configured() is True
    for overrides in (
        {"token": " "},
        {"base_url": "http://b2b.taxi.yandex.net"},
        {"currency": "uzs"},
        {"language": "uz"},
    ):
        assert yandex(Script(), **overrides).is_configured() is False


def test_the_registry_follows_the_mode() -> None:
    with override_settings(YANDEX_DELIVERY_MODE="fake"):
        assert isinstance(get_yandex_delivery({"price": 1}), FakeYandexDelivery)
    with override_settings(
        YANDEX_DELIVERY_MODE="live", YANDEX_DELIVERY_TOKEN=TOKEN, YANDEX_DELIVERY_LANGUAGE="en"
    ):
        adapter = get_yandex_delivery()
        assert isinstance(adapter, LiveYandexDelivery)
        assert (adapter.base_url, adapter.currency, adapter.language) == (BASE, "UZS", "en")
        assert adapter.is_configured() is True
    with (
        override_settings(YANDEX_DELIVERY_MODE="sandbox"),
        pytest.raises(ValueError, match="YANDEX_DELIVERY_MODE"),
    ):
        get_yandex_delivery()
