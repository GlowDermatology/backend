"""Production settings of the live adapters (M3): a live provider refuses to start without the values its
adapter sends; a fake one needs none; no message ever echoes a secret."""

# ruff: noqa: F811 - the production fixtures are imported from the M1 settings tests and requested by name

import json
from collections.abc import Callable
from pathlib import Path
from types import ModuleType

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec, rsa
from django.core.exceptions import ImproperlyConfigured

from glowderma.core.tests.test_settings import (  # noqa: F401 - fixtures shared with the M1 settings tests
    load_fresh_production,
    production_env,
)

LIVE_ENV: dict[str, dict[str, str]] = {
    "CLICK_MODE": {
        "CLICK_SERVICE_ID": "36123",
        "CLICK_MERCHANT_ID": "20456",
        "CLICK_MERCHANT_USER_ID": "40789",
    },
    "PAYME_MODE": {"PAYME_MERCHANT_ID": "587f72c72cac0d162c722ae2"},
    "BTS_MODE": {
        "BTS_LOGIN": "glowderma",
        "BTS_PASSWORD": "bts-password-value",
        "BTS_SENDER_NAME": "Glow Derma",
        "BTS_SENDER_PHONE": "+998712000000",
        "BTS_SENDER_ADDRESS": "Toshkent sh., Amir Temur 1",
        "BTS_SENDER_CITY_CODE": "0101",
    },
    "ESKIZ_MODE": {
        "ESKIZ_EMAIL": "sms@glowderma.uz",
        "ESKIZ_PASSWORD": "eskiz-password-value",
        "ESKIZ_SENDER": "GlowDerma",
    },
    "FCM_MODE": {"FCM_PROJECT_ID": "glow-derma"},
}
SECRET_KEY_LINE = "PRIVATE-KEY-MATERIAL-MUST-NOT-LEAK"  # noqa: S105 - a test marker


def service_account(
    path: Path, key: rsa.RSAPrivateKey | ec.EllipticCurvePrivateKey | None = None, **overrides: object
) -> Path:
    private = key if key is not None else rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = private.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    ).decode()
    document = {
        "type": "service_account",
        "project_id": "glow-derma",
        "private_key_id": "kid-1",
        "private_key": pem,
        "client_email": "fcm-sender@glow-derma.iam.gserviceaccount.com",
        "token_uri": "https://oauth2.googleapis.com/token",
        **overrides,
    }
    path.write_text(json.dumps(document))
    return path


@pytest.fixture
def live(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Callable[[str], None]:
    """Switch one provider live with every value it needs."""

    def switch(mode: str) -> None:
        monkeypatch.setenv(mode, "live")
        for name, value in LIVE_ENV[mode].items():
            monkeypatch.setenv(name, value)
        if mode == "FCM_MODE":
            monkeypatch.setenv("FCM_CREDENTIALS_FILE", str(service_account(tmp_path / "fcm.json")))

    return switch


def test_fake_providers_need_no_provider_values(
    production_env: None, load_fresh_production: Callable[[], ModuleType], monkeypatch: pytest.MonkeyPatch
) -> None:
    for mode, names in LIVE_ENV.items():
        monkeypatch.delenv(mode, raising=False)
        for name in names:
            monkeypatch.delenv(name, raising=False)
    production = load_fresh_production()
    assert {production.CLICK_MODE, production.PAYME_MODE, production.BTS_MODE} == {"fake"}
    assert production.ESKIZ_MODE == production.FCM_MODE == "fake"


@pytest.mark.parametrize("mode", sorted(LIVE_ENV))
def test_a_live_provider_starts_with_its_values(
    production_env: None,
    load_fresh_production: Callable[[], ModuleType],
    live: Callable[[str], None],
    mode: str,
) -> None:
    live(mode)
    assert getattr(load_fresh_production(), mode) == "live"


@pytest.mark.parametrize(
    ("mode", "missing"), [(mode, name) for mode, names in sorted(LIVE_ENV.items()) for name in sorted(names)]
)
def test_a_live_provider_refuses_to_start_without_each_value(
    production_env: None,
    load_fresh_production: Callable[[], ModuleType],
    live: Callable[[str], None],
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
    missing: str,
) -> None:
    live(mode)
    monkeypatch.delenv(missing)
    with pytest.raises(ImproperlyConfigured, match=missing):
        load_fresh_production()


@pytest.mark.parametrize(
    ("mode", "name", "value"),
    [
        ("CLICK_MODE", "CLICK_SERVICE_ID", "00000"),
        ("CLICK_MODE", "CLICK_MERCHANT_USER_ID", "change-me"),
        ("CLICK_MODE", "CLICK_MERCHANT_ID", "20 456"),
        ("CLICK_MODE", "CLICK_MERCHANT_API_URL", "http://api.click.uz/v2/merchant"),
        ("CLICK_MODE", "CLICK_PAY_URL", "my.click.uz/services/pay"),
        ("PAYME_MODE", "PAYME_MERCHANT_ID", "change-me-payme-merchant-id"),
        ("PAYME_MODE", "PAYME_MERCHANT_ID", "587f;ac.order_id=1"),
        ("PAYME_MODE", "PAYME_CHECKOUT_URL", "http://checkout.paycom.uz"),
        ("BTS_MODE", "BTS_PASSWORD", "change-me"),
        ("BTS_MODE", "BTS_SENDER_PHONE", "998712000000"),
        ("BTS_MODE", "BTS_BASE_URL", "http://api.bts.uz"),
        ("ESKIZ_MODE", "ESKIZ_PASSWORD", "change-me-eskiz-password"),
        ("ESKIZ_MODE", "ESKIZ_BASE_URL", "http://notify.eskiz.uz/api"),
        ("ESKIZ_MODE", "ESKIZ_CALLBACK_URL", "http://glowderma.uz/sms"),
        ("FCM_MODE", "FCM_PROJECT_ID", "Glow Derma"),
    ],
)
def test_a_live_provider_refuses_placeholders_and_malformed_values(
    production_env: None,
    load_fresh_production: Callable[[], ModuleType],
    live: Callable[[str], None],
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
    name: str,
    value: str,
) -> None:
    live(mode)
    monkeypatch.setenv(name, value)
    with pytest.raises(ImproperlyConfigured, match=name) as refusal:
        load_fresh_production()
    if "password" in name.lower():
        assert value not in str(refusal.value)


def test_branch_pickup_needs_the_branch_code(
    production_env: None,
    load_fresh_production: Callable[[], ModuleType],
    live: Callable[[str], None],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    live("BTS_MODE")
    monkeypatch.setenv("BTS_PICKUP_TYPE", "branch")
    with pytest.raises(ImproperlyConfigured, match="BTS_SENDER_BRANCH_CODE"):
        load_fresh_production()
    monkeypatch.setenv("BTS_SENDER_BRANCH_CODE", "0124")
    production = load_fresh_production()
    assert (production.BTS_PICKUP_TYPE, production.BTS_SENDER_BRANCH_CODE) == ("branch", "0124")
    monkeypatch.setenv("BTS_PICKUP_TYPE", "self")
    with pytest.raises(ImproperlyConfigured, match="BTS_PICKUP_TYPE"):
        load_fresh_production()


@pytest.mark.parametrize("problem", ["missing", "not_json", "wrong_type", "no_email", "ec_key", "bad_pem"])
def test_live_fcm_needs_a_readable_rsa_service_account(
    production_env: None,
    load_fresh_production: Callable[[], ModuleType],
    live: Callable[[str], None],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    problem: str,
) -> None:
    live("FCM_MODE")
    path = tmp_path / "broken.json"
    if problem == "not_json":
        path.write_text(f"not json {SECRET_KEY_LINE}")
    elif problem == "wrong_type":
        service_account(path, type="authorized_user")
    elif problem == "no_email":
        service_account(path, client_email="")
    elif problem == "ec_key":
        service_account(path, key=ec.generate_private_key(ec.SECP256R1()))
    elif problem == "bad_pem":
        service_account(path, private_key=f"-----BEGIN PRIVATE KEY-----\n{SECRET_KEY_LINE}\n-----END")
    monkeypatch.setenv("FCM_CREDENTIALS_FILE", str(path))
    with pytest.raises(ImproperlyConfigured, match="FCM_CREDENTIALS_FILE") as refusal:
        load_fresh_production()
    assert SECRET_KEY_LINE not in str(refusal.value)
    # no chained exception carries the file's contents into a traceback
    assert refusal.value.__cause__ is None
    assert refusal.value.__context__ is None or refusal.value.__suppress_context__


def test_payme_test_mode_selects_the_sandbox_checkout(
    production_env: None, load_fresh_production: Callable[[], ModuleType], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("PAYME_CHECKOUT_URL", raising=False)
    assert load_fresh_production().PAYME_CHECKOUT_URL == "https://checkout.paycom.uz"
    monkeypatch.setenv("PAYME_TEST_MODE", "true")
    assert load_fresh_production().PAYME_CHECKOUT_URL == "https://test.paycom.uz"
    monkeypatch.setenv("PAYME_CHECKOUT_URL", "https://checkout.example.uz")
    assert load_fresh_production().PAYME_CHECKOUT_URL == "https://checkout.example.uz"
