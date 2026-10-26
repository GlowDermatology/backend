"""Pure Click / Payme protocol helpers, the M2 fake additions (Click status scenario and recorded calls, Payme
checkout URL, BTS regions/quote/track) and ``set_state_data`` (plan §3 P2, customer contract §2.13–§2.14)."""

from __future__ import annotations

import base64
from collections.abc import Iterator

import pytest
from django.test import override_settings

from glowderma.integrations import services
from glowderma.integrations.bts import fake as fake_bts
from glowderma.integrations.bts.ports import BtsRejected, BtsUnavailable, CreateRequest, Parcel
from glowderma.integrations.bts.registry import get_bts
from glowderma.integrations.click import fake as fake_click
from glowderma.integrations.click import protocol as click
from glowderma.integrations.click import sandbox as click_sandbox
from glowderma.integrations.click.ports import ClickUnavailable
from glowderma.integrations.click.registry import get_click
from glowderma.integrations.models import IntegrationState
from glowderma.integrations.payme import protocol as payme
from glowderma.integrations.payme.registry import get_payme


@pytest.fixture(autouse=True)
def clean() -> Iterator[None]:
    fake_click.reset()
    fake_bts.reset()
    yield
    fake_click.reset()
    fake_bts.reset()


def test_click_signature_amount_and_fingerprint() -> None:
    params = click_sandbox.prepare_params(click_trans_id=1, merchant_trans_id="10248-abc", amount="784400")
    assert click.sign_ok(params, "local-click-secret-key", complete=False)
    assert not click.sign_ok(params, "other", complete=False)
    assert not click.sign_ok(params, "", complete=False)
    assert not click.missing_or_malformed(params, complete=False)
    assert click.missing_or_malformed({**params, "click_trans_id": "x"}, complete=False)
    assert click.missing_or_malformed(params, complete=True)  # no merchant_prepare_id
    assert [click.parse_amount(v) for v in ("784400", "784400.00", "784400.5", "abc", "-1", "01")] == [
        784400,
        784400,
        None,
        None,
        None,
        None,
    ]
    resigned = click_sandbox.prepare_params(
        click_trans_id=1, merchant_trans_id="10248-abc", amount="784400", sign_time="2026-01-01 00:00:00"
    )
    assert click.fingerprint(params) == click.fingerprint(resigned)
    assert click.fingerprint(params) != click.fingerprint({**params, "amount": "1"})
    assert click.response(params, click.BAD_AMOUNT, complete=False)["merchant_prepare_id"] is None
    assert click.valid_sign_time("2026-10-23 14:03:00") and not click.valid_sign_time("x")
    assert click.ERRORS[click.SIGN_FAILED] == "SIGN CHECK FAILED!"


def test_click_pay_url() -> None:
    url = get_click().pay_url("10248-abc", 784400, "https://glowderma.uz/pay/return?order_id=10248")
    assert url.startswith("https://my.click.uz/services/pay?service_id=00000&merchant_id=00000&amount=784400")
    assert "transaction_param=10248-abc" in url
    assert "return_url=https%3A%2F%2Fglowderma.uz%2Fpay%2Freturn%3Forder_id%3D10248" in url


def test_click_fake_status_scenario_and_recorded_calls() -> None:
    assert get_click({"status": "reversed"}).payment_status("55") == "reversed"
    assert get_click({"status": "paid", "refund": "succeeded"}).payment_status("55") == "paid"
    with pytest.raises(ClickUnavailable):
        get_click({"status": "down"}).payment_status("55")
    get_click().refund("55", 1)
    assert fake_click.calls[-1] == ("refund", "55")
    with override_settings(DEV_FEATURES_ALLOWED=False):
        get_click().payment_status("56")
    assert ("payment_status", "56") not in fake_click.calls  # nothing recorded in production
    assert fake_click.paydoc_id(10248) == "5500010248" and fake_click.paydoc_id(1, {"paydoc_id": 9}) == "9"


def test_payme_protocol() -> None:
    header = payme.basic_auth("secret")
    assert payme.auth_ok(header, "secret") and not payme.auth_ok(header, "other")
    assert not payme.auth_ok("Basic !!!", "secret") and not payme.auth_ok("Bearer x", "secret")
    assert not payme.auth_ok(header, "")
    samples: list[object] = [5, True, 5.0, "5"]
    assert [payme.is_int(value) for value in samples] == [True, False, False, False]
    error = payme.rpc_error(7, payme.ERR_ORDER_NOT_FOUND, "order_id")
    assert error["error"]["message"]["ru"] == "Заказ не найден." and error["error"]["data"] == "order_id"
    url = get_payme().checkout_url("10248-abc", 78440000, "uz", "https://glowderma.uz/pay/return?order_id=1")
    raw = base64.b64decode(url.removeprefix("https://checkout.paycom.uz/")).decode()
    assert raw.startswith("m=change-me-payme-merchant-id;") or raw.startswith("m=")
    assert "ac.order_id=10248-abc;a=78440000;l=uz;c=https%3A%2F%2F" in raw and raw.endswith(";ct=15000")


def test_bts_fake_regions_quote_and_track() -> None:
    bts = get_bts()
    assert [region.code for region in bts.regions()] == ["10", "11", "12", "13"]
    with pytest.raises(BtsRejected):
        get_bts({"quote": "rejected"}).quote("10", "1001")
    with pytest.raises(BtsUnavailable):
        get_bts({"quote": "down"}).quote("10", "1001")
    with pytest.raises(BtsRejected):
        get_bts({"track": "rejected"}).track("1")
    with pytest.raises(BtsUnavailable):
        get_bts({"track": "down"}).track("1")
    assert bts.track("unknown").status == "100"
    assert get_bts({"track_status": "300", "track_status_at": "t"}).track("1").status_at == "t"


def test_bts_fake_in_production() -> None:
    with override_settings(DEV_FEATURES_ALLOWED=False, BTS_MODE="fake"):
        bts = get_bts({"tariff": 5})
        assert bts.regions() == [] and bts.find_by_client_id("1") is None
        with pytest.raises(BtsRejected):
            bts.create(CreateRequest("1", 1, "n", "+998900000000", "a", "10", "1001"))
        with pytest.raises(BtsUnavailable):
            bts.cancel(Parcel("1", "t", "b", 1, "100"))
    assert fake_bts._parcels == {}


@pytest.mark.django_db
def test_set_state_data_merges_one_key() -> None:
    services.record_failure("bts", "x")
    services.set_state_data("bts", "poll_cursor", 41)
    services.set_state_data("bts", "other", {"a": 1})
    state = IntegrationState.objects.get(provider="bts")
    assert state.data == {"poll_cursor": 41, "other": {"a": 1}} and state.consecutive_failures == 1
    services.set_state_data("bts", "poll_cursor", 42)
    assert IntegrationState.objects.get(provider="bts").data["poll_cursor"] == 42
