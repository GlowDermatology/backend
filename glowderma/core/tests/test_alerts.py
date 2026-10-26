"""core.alerts: the optional alert webhook (docs/ops/monitoring.md) — queueing, dedupe, scrubbing, delivery,
and the outbox.dead wiring."""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any

import httpx
import pytest
from django.db import transaction
from django.test import override_settings

from glowderma.core import alerts, outbox, timeutils
from glowderma.core.models import OutboxMessage
from glowderma.core.tasks import dispatch_outbox

pytestmark = pytest.mark.django_db

URL = "https://alerts.example.uz/hook/s3cr3t-path-token"
SECRET = "alert-webhook-secret-at-least-32-characters"  # noqa: S105
INSTANT = datetime.fromisoformat("2026-10-27T03:40:00+05:00")


@pytest.fixture
def enabled(monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.setattr(timeutils, "now", lambda: INSTANT)
    with override_settings(ALERT_WEBHOOK_URL=URL, ALERT_WEBHOOK_SECRET=SECRET, ALERT_ENV="staging"):
        yield


class FakePost:
    """Stands in for ``httpx.post``: records the calls and answers ``status`` (or raises ``error``)."""

    def __init__(self, status: int = 200, error: Exception | None = None) -> None:
        self.status = status
        self.error = error
        self.calls: list[dict[str, Any]] = []

    def __call__(self, url: str, **kwargs: Any) -> httpx.Response:
        self.calls.append({"url": url, **kwargs})
        if self.error is not None:
            raise self.error
        return httpx.Response(self.status, request=httpx.Request("POST", url))


def alert_messages() -> list[OutboxMessage]:
    return list(OutboxMessage.objects.filter(topic=alerts.ALERT_TOPIC).order_by("id"))


def test_disabled_without_a_url_queues_nothing() -> None:
    with override_settings(ALERT_WEBHOOK_URL=""):
        assert alerts.enabled() is False
        assert alerts.alert("report.consistency_mismatch", {"counts": {"k1": 1}}) is False
    assert alert_messages() == []


def test_alert_queues_one_scrubbed_message(enabled: None) -> None:
    assert alerts.alert(
        "billz.sale_dead",
        {
            "order_id": 10248,
            "purchase_id": 7,
            "phone": "+998901234567",
            "customer_name": "Malika Karimova",
            "fcm_token": "abc",
            "note": "call +998 90 123 45 67 now\x00",
            "nested": {"ids": [1, 2], "Email": "m@x.uz", "deep": {"deeper": {"x": 1}}},
            "at": INSTANT,
            "ratio": 0.5,
            "flag": True,
            "nothing": None,
            "other": object(),
        },
    )
    (message,) = alert_messages()
    assert message.dedupe_key == f"alert:billz.sale_dead::{int(INSTANT.timestamp()) // 1800}"
    payload = message.payload
    assert payload["event"] == "billz.sale_dead"
    assert payload["at"] == "2026-10-27T03:40:00+05:00"
    assert payload["host"]
    fields = payload["fields"]
    assert fields["order_id"] == 10248 and fields["purchase_id"] == 7
    assert fields["phone"] == fields["customer_name"] == fields["fcm_token"] == alerts.REDACTED
    assert fields["note"] == alerts.REDACTED  # free text never leaves, scrubbed or not
    assert fields["nested"]["ids"] == [1, 2] and fields["nested"]["Email"] == alerts.REDACTED
    assert fields["nested"]["deep"] == {"deeper": alerts.REDACTED}  # nesting cut after three levels
    assert fields["at"] == alerts.REDACTED  # a date is not on the allow-list
    assert (fields["ratio"], fields["flag"], fields["nothing"]) == (0.5, True, None)
    assert fields["other"] == alerts.REDACTED
    assert "+998" not in json.dumps(payload)


def test_one_message_per_event_and_scope_per_window(enabled: None, monkeypatch: pytest.MonkeyPatch) -> None:
    assert alerts.alert("outbox.dead", {"message_id": 1}, scope="refund.submit")
    assert alerts.alert("outbox.dead", {"message_id": 2}, scope="refund.submit")  # same window: deduplicated
    assert alerts.alert("outbox.dead", {"message_id": 3}, scope="bts.create_shipment")  # another scope
    assert len(alert_messages()) == 2
    later = INSTANT + timedelta(minutes=30)
    monkeypatch.setattr(timeutils, "now", lambda: later)
    assert alerts.alert("outbox.dead", {"message_id": 4}, scope="refund.submit")  # the next window
    assert [m.payload["fields"]["message_id"] for m in alert_messages()] == [1, 3, 4]
    with override_settings(ALERT_DEDUPE_MINUTES=5):
        monkeypatch.setattr(timeutils, "now", lambda: later + timedelta(minutes=5))
        assert alerts.alert("outbox.dead", {"message_id": 5}, scope="refund.submit")
    assert len(alert_messages()) == 4


def test_a_failing_enqueue_is_logged_and_never_breaks_the_callers_transaction(
    enabled: None, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    def broken(*args: Any, **kwargs: Any) -> OutboxMessage:
        OutboxMessage.objects.create(topic="x", payload={}, dedupe_key="dup")
        OutboxMessage.objects.create(topic="x", payload={}, dedupe_key="dup")  # IntegrityError
        raise AssertionError("unreachable")

    monkeypatch.setattr(outbox, "enqueue", broken)
    with transaction.atomic():
        OutboxMessage.objects.create(topic="caller", payload={}, dedupe_key="caller:1")
        with caplog.at_level(logging.WARNING, logger="glowderma.core.alerts"):
            assert alerts.alert("billz.circuit_open", {"failures": 5}) is False
        # the caller's transaction is still usable and keeps its own write
        assert OutboxMessage.objects.filter(dedupe_key="caller:1").exists()
    assert not OutboxMessage.objects.filter(dedupe_key="dup").exists()
    assert "alert.enqueue_failed event=billz.circuit_open error=IntegrityError" in caplog.text


def send(message: OutboxMessage, post: FakePost, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(httpx, "post", post)
    alerts.send_alert(message)


def test_send_posts_the_signed_compact_document(enabled: None, monkeypatch: pytest.MonkeyPatch) -> None:
    alerts.alert("report.consistency_mismatch", {"counts": {"k3_refund_open": 2}})
    (message,) = alert_messages()
    post = FakePost(204)
    send(message, post, monkeypatch)
    (call,) = post.calls
    body = call["content"]
    assert call["url"] == URL
    assert call["timeout"] == 5.0 and call["follow_redirects"] is False
    assert json.loads(body) == {
        "event": "report.consistency_mismatch",
        "env": "staging",
        "at": "2026-10-27T03:40:00+05:00",
        "fields": {"counts": {"k3_refund_open": 2}},
        "host": message.payload["host"],
    }
    assert b" " not in body  # compact
    expected = hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()
    assert call["headers"][alerts.SIGNATURE_HEADER] == f"sha256={expected}"
    assert call["headers"]["Content-Type"] == "application/json"


def test_send_without_a_secret_has_no_signature_header(monkeypatch: pytest.MonkeyPatch) -> None:
    with override_settings(ALERT_WEBHOOK_URL=URL, ALERT_WEBHOOK_SECRET=""):
        alerts.alert("billz.sale_dead", {"order_id": 1})
        post = FakePost(200)
        send(alert_messages()[0], post, monkeypatch)
    assert alerts.SIGNATURE_HEADER not in post.calls[0]["headers"]


def test_a_url_removed_after_queueing_sends_nothing(enabled: None, monkeypatch: pytest.MonkeyPatch) -> None:
    alerts.alert("billz.sale_dead", {"order_id": 1})
    post = FakePost(200)
    with override_settings(ALERT_WEBHOOK_URL=""):
        send(alert_messages()[0], post, monkeypatch)
    assert post.calls == []


@pytest.mark.parametrize("status", [408, 429, 500, 503])
def test_transient_answers_are_retried(enabled: None, monkeypatch: pytest.MonkeyPatch, status: int) -> None:
    alerts.alert("billz.sale_dead", {"order_id": 1})
    with pytest.raises(alerts.AlertDeliveryError, match=f"HTTP {status}"):
        send(alert_messages()[0], FakePost(status), monkeypatch)


@pytest.mark.parametrize("status", [301, 400, 401, 404])
def test_refusals_are_permanent(enabled: None, monkeypatch: pytest.MonkeyPatch, status: int) -> None:
    alerts.alert("billz.sale_dead", {"order_id": 1})
    with pytest.raises(outbox.PermanentFailure, match=f"HTTP {status}"):
        send(alert_messages()[0], FakePost(status), monkeypatch)


def test_a_transport_error_never_carries_the_url(
    enabled: None, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    alerts.alert("billz.sale_dead", {"order_id": 1})
    error = httpx.ConnectError(f"cannot reach {URL}")
    with caplog.at_level(logging.DEBUG), pytest.raises(alerts.AlertDeliveryError) as raised:
        send(alert_messages()[0], FakePost(error=error), monkeypatch)
    assert str(raised.value) == "alert webhook unreachable (ConnectError)"
    assert raised.value.__cause__ is None and raised.value.__suppress_context__
    assert "s3cr3t" not in caplog.text


def test_the_dispatcher_delivers_and_retries_through_the_outbox(
    enabled: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    alerts.alert("billz.sale_dead", {"order_id": 1})
    post = FakePost(502)
    monkeypatch.setattr(httpx, "post", post)
    dispatch_outbox(batch=1)
    message = alert_messages()[0]
    assert (message.status, message.attempts, message.last_error) == (
        "failed",
        1,
        "alert webhook answered HTTP 502",
    )
    post.status = 200
    OutboxMessage.objects.filter(pk=message.pk).update(available_at=INSTANT)
    dispatch_outbox(batch=1)
    message.refresh_from_db()
    assert message.status == "done" and len(post.calls) == 2


def test_every_dead_topic_alerts_once_per_window(enabled: None, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(outbox, "_handlers", {**outbox._handlers})
    monkeypatch.setattr(outbox, "_dead_handlers", {})
    seen: list[int] = []

    @outbox.register("test.dies")
    def handler(message: OutboxMessage) -> None:
        raise outbox.PermanentFailure("refused for good")

    outbox.on_dead("test.dies")(lambda message: seen.append(message.pk))
    first = OutboxMessage.objects.create(topic="test.dies", payload={}, dedupe_key="test.dies:1")
    OutboxMessage.objects.create(topic="test.dies", payload={}, dedupe_key="test.dies:2")
    monkeypatch.setattr(httpx, "post", FakePost(200))
    dispatch_outbox(batch=2)
    assert seen == [first.pk, first.pk + 1]  # the topic hook still runs for each
    (alert,) = alert_messages()
    assert alert.payload["event"] == "outbox.dead"
    assert alert.payload["fields"] == {"topic": "test.dies", "message_id": first.pk, "attempts": 1}
    assert alert.dedupe_key.startswith("alert:outbox.dead:test.dies:")


def test_a_dead_alert_raises_no_alert_of_its_own(enabled: None, monkeypatch: pytest.MonkeyPatch) -> None:
    alerts.alert("billz.sale_dead", {"order_id": 1})
    monkeypatch.setattr(httpx, "post", FakePost(404))
    dispatch_outbox(batch=5)
    (message,) = alert_messages()
    assert message.status == "dead"
    assert not OutboxMessage.objects.filter(dedupe_key__startswith="alert:outbox.dead").exists()


def test_a_failing_refresh_skips_the_alert_and_the_hook(
    enabled: None, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    message = OutboxMessage.objects.create(topic="gone", payload={}, dedupe_key="gone:1", status="dead")
    OutboxMessage.objects.filter(pk=message.pk).delete()
    with caplog.at_level(logging.ERROR, logger="glowderma.core.outbox"):
        outbox.notify_dead(message)
    assert alert_messages() == []
    assert "Dead-letter hook failed topic=gone" in caplog.text


def test_scrub_fields_bounds_keys_and_lists() -> None:
    many = {f"k{i}": i for i in range(80)}
    assert len(alerts.scrub_fields(many)) == alerts.MAX_ITEMS
    assert alerts.scrub_fields({"ids": list(range(80))})["ids"] == list(range(50))
    assert alerts.scrub_fields({"text": "x" * 64, "long": "x" * 65}) == {
        "text": "x" * 64,
        "long": alerts.REDACTED,
    }
    assert alerts.scrub_fields(None) == {}


PHONE = "+998901234567"


@pytest.mark.parametrize(
    "fields",
    [
        {"contact": "malika@gmail.com"},
        {"note": "malika@gmail.com"},
        {"who": "Malika Karimova"},
        {"auth": "Bearer eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJhYmMifQ.c2lnbmF0dXJl"},
        {"authorization": "Basic cGF5bWU6a2V5"},
        {"api_key": "sk_live_abcdefABCDEF"},
        {"msisdn_int": 998901234567},
        {"count": 998901234567},
        {"count": -998901234567},
        {"count": 1_000_000},
        {"ratio": 998901234567.0},
        {"code": "90_123_45_67"},
        {"code": "998/90/123/45/67"},
        {"code": "998.90.123.45.67"},
        {"code": "tx1234567"},
        {"phone_plain": PHONE},
        {"customer": PHONE},
        {"customer": "+998 (90) 123-45-67"},
        {"fio": "Karimova Malika"},
        {"fio": "Malika"},
        {"otp": "123456"},
        {"otp_code": 123456},
        {"pin": "1234"},
        {"pinCode": 1234},
        {"cvv": "123"},
        {"pan": "8600123456789012"},
        {"card_last4": "9012"},
        {"secret": "s"},
        {"password": "p"},
        {"credentials": "c"},
        {"refresh_token": "t"},
        {"tel": "t"},
        {"mobile": "m"},
        {"first_name": "Malika"},
        {"client_ip": "10.0.0.1"},
        {"address": "Tashkent"},
        {"recipient": {"full": "Malika"}},
        {"nested": {"recipient": {"full": "Malika"}}},
        {"nested": {"recipient": {"tel": PHONE, "full": "Malika"}}},
        {"list": ["malika@gmail.com", PHONE, "Malika Karimova", 998901234567]},
        {"decimal": Decimal("5")},
        {"bytes": b"x"},
        {"day": date(1990, 5, 17)},
        {"set": {"a"}},
    ],
)
def test_nothing_personal_or_secret_leaves(fields: dict[str, Any]) -> None:
    clean = alerts.scrub_fields(fields)
    text = json.dumps(clean)
    for needle in (
        "malika",
        "karimova",
        "998",
        "90123",
        "eyj",
        "sk_live",
        "basic",
        "bearer",
        "123456",
        "1234",
        "9012",
        "tashkent",
        "10.0.0.1",
        "1990",
    ):
        assert needle not in text.lower(), (fields, clean)
    assert set(clean) == set(fields)  # the (safe) key stays, its value is masked
    assert all(value == alerts.REDACTED or isinstance(value, dict | list) for value in clean.values())


@pytest.mark.parametrize(
    "fields",
    [
        {PHONE: "x"},
        {"malika@gmail.com": 1},
        {"Malika Karimova": 1},
        {"90_123_45_67": 1},
        {"k" * 65: 1},
        {1: 2},
    ],
)
def test_keys_that_are_not_short_codes_are_dropped(fields: dict[Any, Any]) -> None:
    assert alerts.scrub_fields({"order_id": 5, **fields}) == {"order_id": 5, alerts.DROPPED_KEYS_FIELD: 1}
    nested = alerts.scrub_fields({"inner": fields})
    assert nested == {"inner": {alerts.DROPPED_KEYS_FIELD: 1}}
    # a caller's own "dropped_keys" cannot hide what was dropped
    assert alerts.scrub_fields({alerts.DROPPED_KEYS_FIELD: 0, **fields}) == {alerts.DROPPED_KEYS_FIELD: 2}


def test_ids_counts_and_codes_pass() -> None:
    fields = {
        "order_id": 10248,
        "message_id": 999_999,
        "negative": -5,
        "ratio": 0.25,
        "flag": False,
        "nothing": None,
        "topic": "billz.push_sale",
        "case": "paid_after_cancel",
        "counts": {"k3_refund_open": 2, "k1_billz_sale_unrecorded": 0},
        "ids": [1, 2, 3],
        "shipment_id": 7,
        "company": "glow",
        "version": "1.2.3",
        "ref": "a:b-c.d_e",
    }
    assert alerts.scrub_fields(fields) == fields


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_non_finite_floats_become_null_and_the_alert_is_queued(enabled: None, value: float) -> None:
    assert alerts.scrub_fields({"ratio": value, "deep": {"x": [value]}}) == {
        "ratio": None,
        "deep": {"x": [None]},
    }
    assert alerts.alert("probe.nan", {"ratio": value}) is True
    (message,) = alert_messages()
    assert message.payload["fields"] == {"ratio": None}
