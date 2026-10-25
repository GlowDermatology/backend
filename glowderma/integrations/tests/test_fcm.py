"""The FCM port: fake outcomes by token suffix, registry by ``FCM_MODE``."""

import pytest
from django.core.exceptions import ImproperlyConfigured
from django.test import override_settings

from glowderma.integrations.fcm import registry
from glowderma.integrations.fcm.fake import FakeFcm
from glowderma.integrations.fcm.ports import UNREGISTERED, FcmUnavailable, PushMessage


def test_fake_accepts_rejects_bad_tokens_and_keeps_order() -> None:
    fcm = FakeFcm()
    results = fcm.send(
        [
            PushMessage("tok-1", "A", "a", {"order_id": "10248"}),
            PushMessage("tok-2-bad", "B", "b"),
            PushMessage("tok-3", "C", "c"),
        ]
    )
    assert [r.ok for r in results] == [True, False, True]
    assert results[1].error == UNREGISTERED and results[1].message_id == ""
    assert results[0].message_id.startswith("fake-") and results[0].message_id.endswith("-0")
    assert results[2].message_id.endswith("-2")
    assert [m.token for m in fcm.sent] == ["tok-1", "tok-3"]
    # deterministic: the same token yields the same id at the same position
    assert fcm.send([PushMessage("tok-1", "A", "a")])[0].message_id == results[0].message_id


def test_fake_down_token_fails_the_whole_batch() -> None:
    fcm = FakeFcm()
    with pytest.raises(FcmUnavailable):
        fcm.send([PushMessage("ok", "A", "a"), PushMessage("x-down", "B", "b")])
    assert fcm.sent == []


def test_registry_returns_the_shared_fake_and_refuses_live() -> None:
    assert registry.get_fcm_port() is registry.get_fcm_port()
    assert isinstance(registry.get_fcm_port(), FakeFcm)
    with override_settings(FCM_MODE="live"), pytest.raises(ImproperlyConfigured, match="FCM_MODE"):
        registry.get_fcm_port()
