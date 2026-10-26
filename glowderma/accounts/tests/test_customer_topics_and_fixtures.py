"""``accounts.topics`` (customer contract §2.14.2) and the customer/POS test fixtures (plan §1 P0)."""

import re
from typing import Any

import pytest
from rest_framework.test import APIClient

from glowderma.accounts import services, topics
from glowderma.accounts.models import AuthSession, Customer
from glowderma.accounts.tests.factories import device_headers, raw_device_key, raw_refresh


def test_topic_names_and_dedupe_keys() -> None:
    assert (topics.SMS_SEND, topics.PUSH_SESSION_REPLACED) == ("sms.send", "push.session_replaced")
    assert (topics.PUSH_ACCOUNT_SECURITY, topics.APPLE_REVOKE) == ("push.account_security", "apple.revoke")
    assert topics.sms_send_key(7) == "sms.send:7"
    assert topics.session_replaced_key("abc") == "push.session_replaced:abc"
    assert (
        topics.account_security_key(5, "password_reset", "e1") == "push.account_security:5:password_reset:e1"
    )
    assert topics.apple_revoke_key(9) == "apple.revoke:9"
    assert topics.account_security_inbox_key(5, "social_linked", "e1") == "account:5:social_linked:e1"


def test_account_security_payload_has_every_key_in_order() -> None:
    payload = topics.account_security_payload(5, "password_reset", farewell_tokens=["tok-a", "", "tok-b"])
    assert tuple(payload) == topics.ACCOUNT_SECURITY_PAYLOAD_KEYS
    assert payload["customer_id"] == 5 and payload["event"] == "password_reset" and payload["provider"] == ""
    assert payload["farewell_tokens"] == ["tok-a", "tok-b"]
    assert re.fullmatch(r"[0-9a-f]{32}", payload["event_id"])
    linked = topics.account_security_payload(5, "social_linked", provider="apple")
    assert (linked["provider"], linked["farewell_tokens"]) == ("apple", [])
    assert linked["event_id"] != payload["event_id"]


@pytest.mark.parametrize(
    ("event", "provider"),
    [("password_reset", "google"), ("social_unlinked", ""), ("social_linked", "twitter"), ("bogus", "")],
)
def test_account_security_payload_refuses_inconsistent_values(event: str, provider: str) -> None:
    with pytest.raises(ValueError):
        topics.account_security_payload(5, event, provider=provider)


@pytest.mark.django_db
def test_customer_fixtures(
    customer: Customer, customer_session: tuple[AuthSession, str], customer_client: APIClient
) -> None:
    session, refresh = customer_session
    assert session.customer_id == customer.pk and raw_refresh(session) == refresh
    assert session.device is not None and session.device.fid == "fid-test-0001"
    assert services.device_is_known(customer.pk, "fid-test-0001", raw_device_key(session.device))
    credentials: Any = vars(customer_client)["_credentials"]
    assert (
        credentials["HTTP_X_GD_DEVICE_ID"] == "fid-test-0001" and credentials["HTTP_X_GD_PLATFORM"] == "ios"
    )
    assert credentials["HTTP_AUTHORIZATION"].startswith("Bearer ")
    assert credentials["HTTP_X_GD_DEVICE_KEY"] == raw_device_key(session.device)


@pytest.mark.django_db
def test_public_and_pos_fixtures(
    public_client: APIClient, pos_client: APIClient, pos_device: Any, pos_key: str
) -> None:
    import hashlib

    assert vars(public_client)["_credentials"]["HTTP_X_GD_DEVICE_ID"] == "fid-public-0001"
    assert "HTTP_AUTHORIZATION" not in vars(public_client)["_credentials"]
    assert re.fullmatch(r"gdpos_[A-Za-z0-9]{8}_[A-Za-z0-9_-]{32}", pos_key)
    assert pos_device.key_hash == hashlib.sha256(pos_key.encode()).hexdigest() and pos_device.is_active
    assert vars(pos_client)["_credentials"]["HTTP_AUTHORIZATION"] == f"Api-Key {pos_key}"


def test_device_headers_helper() -> None:
    assert device_headers() == {
        "HTTP_X_GD_DEVICE_ID": "fid-test-0001",
        "HTTP_X_GD_PLATFORM": "ios",
        "HTTP_X_GD_APP_VERSION": "1.0.0",
    }
    assert device_headers(fid="f" * 8, platform="android", version="2.0.0", key="k" * 43)[
        "HTTP_X_GD_DEVICE_KEY"
    ] == ("k" * 43)
