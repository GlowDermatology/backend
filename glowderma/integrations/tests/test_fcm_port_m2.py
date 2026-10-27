"""The extended FCM port (customer contract §2.14.2): message fields, error mapping, production posture."""

import logging

import pytest
from django.test import override_settings

from glowderma.integrations.fcm.fake import FakeFcm
from glowderma.integrations.fcm.ports import PAYLOAD_INVALID, UNREGISTERED, FcmUnavailable, PushMessage


def test_push_message_defaults_and_data_only_variant() -> None:
    message = PushMessage("tok", "Title", "Body", {"type": "order_status"})
    assert (message.data_only, message.android_channel, message.android_priority) == (False, "", "high")
    assert (message.apns_push_type, message.apns_priority, message.image_url, message.collapse_key) == (
        "alert",
        10,
        "",
        "",
    )
    data = PushMessage.data_message("tok", {"type": "session_replaced", "sid": "s"})
    assert data.data_only and data.title == data.body == ""
    assert (data.apns_push_type, data.apns_priority, data.android_priority) == ("background", 5, "high")
    with pytest.raises(TypeError):
        PushMessage("tok", "t", "b", {}, True)  # type: ignore[call-arg]  # the extras are keyword-only


def test_fake_records_every_field_under_the_local_test_settings() -> None:
    fcm = FakeFcm()
    message = PushMessage(
        "tok-1",
        "T",
        "B",
        {"type": "campaign", "inbox_id": "9"},
        android_channel="promo",
        android_priority="normal",
        image_url="https://media.example.uz/c.webp",
        collapse_key="campaign-1",
    )
    [result] = fcm.send([message])
    assert result.ok and result.message_id.startswith("fake-")
    assert fcm.sent == [message]
    assert fcm.sent[0].android_channel == "promo" and fcm.sent[0].image_url.endswith("c.webp")


def test_fake_error_mapping() -> None:
    fcm = FakeFcm()
    results = fcm.send(
        [PushMessage("x-bad", "t", "b"), PushMessage("x-payload", "t", "b"), PushMessage("ok", "t", "b")]
    )
    assert [(r.ok, r.error) for r in results] == [(False, UNREGISTERED), (False, PAYLOAD_INVALID), (True, "")]
    assert len(fcm.sent) == 1
    with pytest.raises(FcmUnavailable):
        fcm.send([PushMessage("ok", "t", "b"), PushMessage("x-down", "t", "b")])


def test_fake_keeps_nothing_under_the_production_settings(caplog: pytest.LogCaptureFixture) -> None:
    fcm = FakeFcm()
    with (
        override_settings(DEV_FEATURES_ALLOWED=False),
        caplog.at_level(logging.INFO, logger="glowderma.integrations.fcm.fake"),
    ):
        results = fcm.send([PushMessage("tok", "Secret title", "Secret body", {"type": "order_status"})] * 3)
    assert all(result.ok for result in results)
    assert fcm.sent == []
    lines = [record.getMessage() for record in caplog.records]
    assert len(lines) == 3 and all(line.startswith("fcm.fake_accepted message_id=fake-") for line in lines)
    assert not any("Secret" in line for line in lines)
