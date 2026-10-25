"""The fake BTS / Click / Payme adapters (rules documented in their modules) and the *_MODE registries."""

from collections.abc import Iterator

import pytest
from django.test import override_settings

from glowderma.integrations.bts import fake as fake_bts
from glowderma.integrations.bts.ports import BtsRejected, BtsUnavailable, CreateRequest, Parcel
from glowderma.integrations.bts.registry import get_bts
from glowderma.integrations.click import fake as fake_click
from glowderma.integrations.click.ports import ClickRefundRejected, ClickUnavailable
from glowderma.integrations.click.registry import get_click
from glowderma.integrations.payme import fake as fake_payme
from glowderma.integrations.payme.registry import get_payme


@pytest.fixture(autouse=True)
def clean_fakes() -> Iterator[None]:
    fake_bts.reset()
    fake_click.reset()
    yield
    fake_bts.reset()
    fake_click.reset()


def _request(client_id: str = "10248", attempt: int = 1, region: str = "10") -> CreateRequest:
    return CreateRequest(client_id, attempt, "Malika Karimova", "+998901234567", "Toshkent", region, "1001")


def test_bts_quote_by_region_and_override() -> None:
    bts = get_bts()
    assert [bts.quote(code, "") for code in ("10", "11", "12", "13", "77")] == [
        25_000,
        26_000,
        27_000,
        28_000,
        25_000,
    ]
    assert get_bts({"tariff": 31_000}).quote("13", "") == 31_000


def test_bts_create_lookup_and_failures() -> None:
    bts = get_bts()
    assert bts.find_by_client_id("10248") is None
    parcel = bts.create(_request())
    assert parcel == Parcel("10248", "48210248", "BTS-10248", 25_000, "100")
    assert bts.find_by_client_id("10248") == parcel  # the redelivery adopts it
    custom = get_bts({"tracking_number": "48213907", "status": "200", "tariff": 27_000}).create(_request("5"))
    assert (custom.tracking_number, custom.status, custom.tariff) == ("48213907", "200", 27_000)
    failing = get_bts({"fail_attempts": 1})
    with pytest.raises(BtsRejected) as rejected:
        failing.create(_request("7", attempt=1))
    assert rejected.value.error_code == "address_not_found"
    assert failing.create(_request("7", attempt=2)).tracking_number == "48200007"
    with pytest.raises(BtsUnavailable):
        get_bts({"down": True}).create(_request("8"))
    with pytest.raises(BtsUnavailable):
        get_bts({"down": True}).find_by_client_id("8")
    assert get_bts({"existing": True}).find_by_client_id("9") is not None


def test_bts_cancel_rules() -> None:
    bts = get_bts()
    parcel = bts.create(_request("11"))
    assert bts.cancel(parcel).ok
    assert get_bts().cancel(Parcel("12", "1", "x", 1, "")).ok  # no status yet
    refused = bts.cancel(Parcel("13", "1", "x", 1, "200"))
    assert (refused.ok, refused.reason, refused.status) == (False, "posilka kuryerda", "200")
    assert not get_bts({"status": "300"}).cancel(parcel).ok
    assert not get_bts({"cancel": "refused"}).cancel(parcel).ok
    with pytest.raises(BtsUnavailable):
        get_bts({"cancel": "unavailable"}).cancel(parcel)


def test_click_refund_scenarios() -> None:
    click = get_click()
    assert click.payment_status("CLK-1") == "paid"
    assert click.refund("CLK-1", 1000).provider_refund_id == "CLKR-1"
    assert click.payment_status("CLK-1") == "reversed"
    with pytest.raises(ClickRefundRejected) as rejected:
        get_click({"refund": "failed"}).refund("CLK-2", 1000)
    assert rejected.value.error_code == "refund_rejected"
    unknown = get_click({"refund": "unknown"})
    assert unknown.payment_status("CLK-3") == "paid"
    with pytest.raises(ClickUnavailable):
        unknown.refund("CLK-3", 1000)
    with pytest.raises(ClickUnavailable):
        unknown.payment_status("CLK-3")
    down = get_click({"refund": "down"})
    for call in (lambda: down.payment_status("CLK-4"), lambda: down.refund("CLK-4", 1)):
        with pytest.raises(ClickUnavailable):
            call()


def test_transaction_ids_follow_the_design_rule() -> None:
    assert fake_click.transaction_id(10248) == "CLK-7741379176"
    assert fake_click.transaction_id(10248, {"transaction_id": "CLK-7741920385"}) == "CLK-7741920385"
    assert fake_payme.transaction_id(10001) == "PME-7741370037"
    assert fake_payme.transaction_id(1, {"transaction_id": "PME-1"}) == "PME-1"
    payme = get_payme()
    assert payme.refunds_are_manual and payme.is_configured()


@pytest.mark.parametrize(
    ("setting", "factory"), [("BTS_MODE", get_bts), ("CLICK_MODE", get_click), ("PAYME_MODE", get_payme)]
)
def test_registries_follow_the_mode_settings(setting: str, factory: object) -> None:
    with override_settings(**{setting: "live"}), pytest.raises(NotImplementedError, match="M3"):
        factory()  # type: ignore[operator]
    with override_settings(**{setting: "bogus"}), pytest.raises(ValueError):
        factory()  # type: ignore[operator]
    with override_settings(**{setting: None, "INTEGRATIONS_MODE": "fake"}):
        assert factory() is not None  # type: ignore[operator]
