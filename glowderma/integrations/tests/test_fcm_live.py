"""The live FCM adapter (customer contract §2.14.2; docs/integrations/fcm.md): the service-account token, the
HTTP v1 message document, one result per message, token pruning only on a dead-token answer, and the batch
rule for transient failures."""

import json
import logging
import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from urllib.parse import parse_qs

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from django.test import override_settings

from glowderma.integrations import httpclient
from glowderma.integrations.fcm import live as fcm_live
from glowderma.integrations.fcm import protocol
from glowderma.integrations.fcm.live import (
    THIRD_PARTY_AUTH_ERROR,
    LiveFcm,
    ServiceAccountError,
    access_tokens,
    load_service_account,
)
from glowderma.integrations.fcm.ports import (
    NOT_SENT,
    PAYLOAD_INVALID,
    UNAVAILABLE,
    UNREGISTERED,
    FcmUnavailable,
    PushMessage,
    PushResult,
)
from glowderma.integrations.fcm.registry import get_fcm_port

from .live_fixtures import Script, connect_error, load, read_timeout, reply

SEND_PATH = "/v1/projects/glow-derma/messages:send"
TOKEN_PATH = "/token"  # noqa: S105 - an endpoint path
EMAIL = "fcm-sender@glow-derma.iam.gserviceaccount.com"


@pytest.fixture(autouse=True)
def fresh_tokens(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setattr(httpclient, "RETRY_BACKOFF_SECONDS", 0)
    access_tokens.clear()
    yield
    access_tokens.clear()


@pytest.fixture
def credentials(tmp_path: Path, rsa_private_pem: bytes) -> Path:
    path = tmp_path / "fcm.json"
    path.write_text(
        json.dumps(
            {
                "type": "service_account",
                "project_id": "glow-derma",
                "private_key_id": "kid-1",
                "private_key": rsa_private_pem.decode(),
                "client_email": EMAIL,
                "token_uri": "https://oauth2.googleapis.com/token",
            }
        )
    )
    return path


def fcm(script: Script, credentials: Path, concurrency: int = 4) -> LiveFcm:
    script.routes.setdefault(("POST", TOKEN_PATH), [reply("fcm", "token_ok")])
    return LiveFcm(
        project_id="glow-derma",
        credentials_file=str(credentials),
        transport=script.transport,
        concurrency=concurrency,
    )


def order_push(token: str = "device-token-1") -> PushMessage:  # noqa: S107 - a device token
    return PushMessage(
        token,
        "Kuryerga topshirildi",
        "Buyurtma №10248 kuryerga topshirildi.",
        {"type": "order_status", "inbox_id": "901", "order_id": "10248", "status": "handed_to_courier"},
        android_channel="orders",
    )


def by_token(answers: dict[str, httpx.Response]) -> Callable[[httpx.Request], httpx.Response]:
    def respond(request: httpx.Request) -> httpx.Response:
        answer = answers[json.loads(request.content)["message"]["token"]]
        return httpx.Response(answer.status_code, content=answer.content, headers=answer.headers)

    return respond


def test_the_token_is_a_service_account_jwt_grant_minted_once(
    credentials: Path, rsa_private_pem: bytes
) -> None:
    script = Script().on("POST", SEND_PATH, reply("fcm", "send_ok"))
    adapter = fcm(script, credentials)
    results = adapter.send([order_push("a"), order_push("b"), order_push("c")])
    assert results == [PushResult(ok=True, message_id="0:1500415314455276%31bd1c9631bd1c96")] * 3
    (grant,) = script.to("POST", TOKEN_PATH)
    assert str(grant.url) == protocol.TOKEN_URL
    form = parse_qs(grant.content.decode())
    assert form["grant_type"] == ["urn:ietf:params:oauth:grant-type:jwt-bearer"]
    public = serialization.load_pem_private_key(rsa_private_pem, password=None).public_key()
    claims = jwt.decode(form["assertion"][0], public, algorithms=["RS256"], audience=protocol.TOKEN_URL)  # type: ignore[arg-type]
    assert claims["iss"] == EMAIL and claims["scope"] == "https://www.googleapis.com/auth/firebase.messaging"
    assert claims["exp"] - claims["iat"] == 3600
    assert jwt.get_unverified_header(form["assertion"][0])["kid"] == "kid-1"
    for send in script.to("POST", SEND_PATH):
        assert send.headers["Authorization"] == "Bearer ya29.fcm-access-token-1"
        assert str(send.url) == f"https://fcm.googleapis.com{SEND_PATH}"
    adapter.send([order_push()])
    assert len(script.to("POST", TOKEN_PATH)) == 1  # cached in the process until shortly before expiry


def test_the_message_document_follows_the_contract() -> None:
    assert protocol.message_document(order_push()) == {
        "message": {
            "token": "device-token-1",
            "notification": {
                "title": "Kuryerga topshirildi",
                "body": "Buyurtma №10248 kuryerga topshirildi.",
            },
            "data": {
                "type": "order_status",
                "inbox_id": "901",
                "order_id": "10248",
                "status": "handed_to_courier",
            },
            "android": {"priority": "high", "notification": {"channel_id": "orders"}},
            "apns": {
                "headers": {"apns-priority": "10", "apns-push-type": "alert"},
                "payload": {"aps": {"sound": "default", "mutable-content": 1}},
            },
        }
    }
    data_only = protocol.message_document(
        PushMessage.data_message("t", {"type": "session_replaced", "sid": "9"}, collapse_key="sid-9")
    )
    assert data_only == {
        "message": {
            "token": "t",
            "data": {"type": "session_replaced", "sid": "9"},
            "android": {"priority": "high", "collapse_key": "sid-9"},
            "apns": {
                "headers": {
                    "apns-priority": "5",
                    "apns-push-type": "background",
                    "apns-collapse-id": "sid-9",
                },
                "payload": {"aps": {"content-available": 1}},
            },
        }
    }
    campaign = PushMessage(
        "t",
        "Aksiya",
        "-20%",
        {"type": "campaign"},
        android_channel="promo",
        android_priority="normal",
        image_url="https://media.glowderma.uz/c.webp",
    )
    document = protocol.message_document(campaign)["message"]
    assert document["notification"]["image"] == "https://media.glowderma.uz/c.webp"
    assert document["android"] == {"priority": "normal", "notification": {"channel_id": "promo"}}


@pytest.mark.parametrize(
    ("fixture", "status", "error"),
    [
        ("error_unregistered", 404, UNREGISTERED),
        ("error_sender_id_mismatch", 403, UNREGISTERED),
        ("error_invalid_token", 400, UNREGISTERED),
        ("error_invalid_payload", 400, PAYLOAD_INVALID),
        ("error_third_party_auth", 401, THIRD_PARTY_AUTH_ERROR),
    ],
)
def test_per_message_answers(credentials: Path, fixture: str, status: int, error: str) -> None:
    script = Script().on(
        "POST",
        SEND_PATH,
        by_token({"ok": reply("fcm", "send_ok"), "other": reply("fcm", fixture, status)}),
    )
    results = fcm(script, credentials).send([order_push("ok"), order_push("other"), order_push("ok")])
    assert [r.ok for r in results] == [True, False, True]
    assert results[1] == PushResult(ok=False, error=error)
    assert len(script.to("POST", TOKEN_PATH)) == 1  # a token-level answer never refreshes our access token


def test_only_a_dead_token_answer_prunes() -> None:
    """Contract §2.14.2: UNREGISTERED, SENDER_ID_MISMATCH or INVALID_ARGUMENT naming the token, no other."""

    dead = {"error_unregistered": 404, "error_sender_id_mismatch": 403, "error_invalid_token": 400}
    alive = {
        "error_invalid_payload": 400,
        "error_quota": 429,
        "error_unavailable": 503,
        "error_internal": 500,
        "error_third_party_auth": 401,
        "error_unauthenticated": 401,
        "error_permission_denied": 403,
        "error_project_not_found": 404,
    }
    for name, status in dead.items():
        assert protocol.classify(status, load("fcm", name)).kind == protocol.DEAD_TOKEN, name
    for name, status in alive.items():
        assert protocol.classify(status, load("fcm", name)).kind != protocol.DEAD_TOKEN, name
    assert protocol.classify(502, None).kind == protocol.TRANSIENT
    assert (
        protocol.classify(400, {"error": {"message": "Request contains an invalid argument."}}).kind
        == protocol.PAYLOAD
    )


@pytest.mark.parametrize(
    "responder",
    [
        reply("fcm", "error_unavailable", 503),
        reply("fcm", "error_internal", 500),
        reply("fcm", "error_quota", 429),
        httpx.Response(502, text="<html>bad gateway</html>"),
        connect_error,
    ],
)
def test_a_batch_nobody_accepted_raises_for_the_outbox_retry(credentials: Path, responder: object) -> None:
    script = Script().on("POST", SEND_PATH, responder)  # type: ignore[arg-type]
    with pytest.raises(FcmUnavailable):
        fcm(script, credentials).send([order_push("a"), order_push("b")])


def test_a_transient_failure_after_an_acceptance_gives_the_rest_back(credentials: Path) -> None:
    """R4: 503 stops the batch; the message FCM refused and the ones never sent are ``not_sent`` (re-queued
    by the caller), never "failed"."""
    script = Script().on("POST", SEND_PATH, reply("fcm", "send_ok"), reply("fcm", "error_unavailable", 503))
    results = fcm(script, credentials, concurrency=1).send(
        [order_push("a"), order_push("b"), order_push("c")]
    )
    assert results[0].ok and results[1:] == [PushResult(ok=False, error=NOT_SENT)] * 2
    assert len(script.to("POST", SEND_PATH)) == 2  # the batch stopped at the first transient failure


def test_a_device_quota_error_does_not_stop_the_batch(credentials: Path) -> None:
    """A-S7: QUOTA_EXCEEDED may be that device's rate: that message is ``not_sent``, the rest go out."""
    script = Script().on(
        "POST",
        SEND_PATH,
        reply("fcm", "send_ok"),
        reply("fcm", "error_quota", 429),
        reply("fcm", "send_ok"),
    )
    results = fcm(script, credentials, concurrency=1).send([order_push(f"t{n}") for n in range(4)])
    assert [r.ok for r in results] == [True, False, True, True] and results[1].error == NOT_SENT
    assert len(script.to("POST", SEND_PATH)) == 4


def test_a_big_batch_with_one_overloaded_answer_fails_nothing_it_never_sent(credentials: Path) -> None:
    """R4: 500 messages, one 503: what was never sent is ``not_sent``, never ``unavailable``."""
    sent: list[str] = []
    lock = threading.Lock()

    def handler(request: httpx.Request) -> httpx.Response:
        token = json.loads(request.content)["message"]["token"]
        with lock:
            sent.append(token)
        if token == "t5":  # noqa: S105 - a device token
            return reply("fcm", "error_unavailable", 503)
        return reply("fcm", "send_ok")

    results = fcm(Script().on("POST", SEND_PATH, handler), credentials, concurrency=8).send(
        [order_push(f"t{n}") for n in range(500)]
    )
    assert all(r.ok or r.error == NOT_SENT for r in results)
    assert {f"t{n}" for n, r in enumerate(results) if r.ok} <= set(sent)
    assert sum(1 for r in results if r.error == NOT_SENT) >= 500 - len(sent)


def test_an_answer_lost_after_sending_is_unknown_and_never_resent(credentials: Path) -> None:
    """R15 / A-M1: a timeout or an undecodable answer after the request left: ``unavailable`` (failed, never
    re-sent blindly), the rest of the batch ``not_sent``; nothing raises."""

    def garbled(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"Content-Encoding": "gzip"}, stream=httpx.ByteStream(b"not gzip"))

    for failure in (read_timeout, garbled):
        script = Script().on("POST", SEND_PATH, failure)
        results = fcm(script, credentials, concurrency=1).send([order_push("a"), order_push("b")])
        assert results == [PushResult(ok=False, error=UNAVAILABLE), PushResult(ok=False, error=NOT_SENT)]
        assert len(script.to("POST", SEND_PATH)) == 1


def test_an_unexpected_error_keeps_the_results_already_accepted(
    credentials: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """R1: one worker's surprise does not discard the batch's accepted results."""
    real = LiveFcm._send_one

    def flaky(self: LiveFcm, http: httpx.Client, message: PushMessage, budget: httpclient.Deadline) -> object:
        if message.token == "t2":  # noqa: S105 - a device token
            raise KeyError("a bug")
        return real(self, http, message, budget)

    monkeypatch.setattr(LiveFcm, "_send_one", flaky)
    script = Script().on("POST", SEND_PATH, reply("fcm", "send_ok"))
    results = fcm(script, credentials, concurrency=1).send([order_push(f"t{n}") for n in range(4)])
    assert [r.ok for r in results[:2]] == [True, True]
    assert results[2:] == [PushResult(ok=False, error=UNAVAILABLE), PushResult(ok=False, error=NOT_SENT)]
    assert "fcm.send unexpected failure" in caplog.text


def test_the_batch_has_a_time_budget(credentials: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """R8: a slow FCM never stretches one batch past BATCH_SECONDS; the rest is ``not_sent``."""
    monkeypatch.setattr(fcm_live, "BATCH_SECONDS", 0.3)
    monkeypatch.setattr(fcm_live, "MIN_SEND_SECONDS", 0.05)

    def slow(request: httpx.Request) -> httpx.Response:
        time.sleep(0.1)
        return reply("fcm", "send_ok")

    started = time.monotonic()
    results = fcm(Script().on("POST", SEND_PATH, slow), credentials, concurrency=1).send(
        [order_push(f"t{n}") for n in range(20)]
    )
    assert time.monotonic() - started < 1.0
    assert results[0].ok and results[-1].error == NOT_SENT


def test_the_production_batch_budget_fits_the_outbox_lease() -> None:
    assert fcm_live.BATCH_SECONDS <= 120 < 300


def test_a_batch_that_all_answers_sender_id_mismatch_is_a_configuration_error(
    credentials: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Nit: a wrong FCM_PROJECT_ID makes every token "mismatch": no device is pruned, ERROR, retried."""
    script = Script().on("POST", SEND_PATH, reply("fcm", "error_sender_id_mismatch", 403))
    with caplog.at_level(logging.ERROR), pytest.raises(FcmUnavailable):
        fcm(script, credentials).send([order_push(f"t{n}") for n in range(5)])
    assert "fcm.sender_id_mismatch" in caplog.text
    few = fcm(script, credentials).send([order_push(f"t{n}") for n in range(4)])  # below the breaker
    assert {r.error for r in few} == {UNREGISTERED}


def test_a_refused_access_token_is_renewed_once(credentials: Path, caplog: pytest.LogCaptureFixture) -> None:
    script = Script().on(
        "POST", SEND_PATH, reply("fcm", "error_unauthenticated", 401), reply("fcm", "send_ok")
    )
    script.on(
        "POST",
        TOKEN_PATH,
        reply("fcm", "token_ok"),
        httpx.Response(200, json={"access_token": "ya29.fcm-access-token-2", "expires_in": 3599}),
    )
    assert fcm(script, credentials).send([order_push()])[0].ok
    assert script.to("POST", SEND_PATH)[1].headers["Authorization"] == "Bearer ya29.fcm-access-token-2"
    script = Script().on("POST", SEND_PATH, reply("fcm", "error_unauthenticated", 401))
    access_tokens.clear()
    with caplog.at_level(logging.ERROR), pytest.raises(FcmUnavailable):
        fcm(script, credentials).send([order_push()])
    assert "fcm.config_error" in caplog.text


@pytest.mark.parametrize(
    "fixture,status", [("error_permission_denied", 403), ("error_project_not_found", 404)]
)
def test_a_project_or_permission_error_never_prunes(
    credentials: Path, caplog: pytest.LogCaptureFixture, fixture: str, status: int
) -> None:
    script = Script().on("POST", SEND_PATH, reply("fcm", fixture, status))
    with caplog.at_level(logging.ERROR), pytest.raises(FcmUnavailable):
        fcm(script, credentials).send([order_push("a"), order_push("b")])
    assert "fcm.config_error" in caplog.text


def test_token_endpoint_and_credential_failures(
    credentials: Path, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    script = Script().on("POST", TOKEN_PATH, reply("fcm", "token_invalid_grant", 400))
    script.on("POST", SEND_PATH, reply("fcm", "send_ok"))
    with caplog.at_level(logging.ERROR), pytest.raises(FcmUnavailable):
        fcm(script, credentials).send([order_push()])
    assert "fcm.auth_failed" in caplog.text and script.to("POST", SEND_PATH) == []
    caplog.clear()
    script = Script().on("POST", TOKEN_PATH, connect_error)
    with caplog.at_level(logging.WARNING), pytest.raises(FcmUnavailable):
        fcm(script, credentials).send([order_push()])
    # nit: an unreachable token endpoint is a WARNING (FCM unavailable), never a configuration ERROR
    assert "fcm.token_unavailable" in caplog.text and "fcm.config_error" not in caplog.text
    with caplog.at_level(logging.ERROR), pytest.raises(FcmUnavailable):
        fcm(Script(), tmp_path / "missing.json").send([order_push()])
    assert "fcm.credentials_invalid" in caplog.text


def test_concurrent_messages_mint_one_token(credentials: Path) -> None:
    def slow_token(request: httpx.Request) -> httpx.Response:
        time.sleep(0.1)
        return reply("fcm", "token_ok")

    script = Script().on("POST", TOKEN_PATH, slow_token).on("POST", SEND_PATH, reply("fcm", "send_ok"))
    adapter = fcm(script, credentials, concurrency=8)
    start = threading.Event()
    outcomes: list[list[PushResult]] = []

    def worker() -> None:
        start.wait(10)
        outcomes.append(adapter.send([order_push(f"t{n}") for n in range(4)]))

    threads = [threading.Thread(target=worker) for _ in range(3)]
    for thread in threads:
        thread.start()
    start.set()
    for thread in threads:
        thread.join(10)
    assert len(outcomes) == 3 and all(r.ok for batch in outcomes for r in batch)
    assert len(script.to("POST", TOKEN_PATH)) == 1


def test_an_empty_batch_sends_nothing(credentials: Path) -> None:
    script = Script()
    assert fcm(script, credentials).send([]) == [] and script.requests == []


def test_no_secret_or_device_token_reaches_the_logs(
    credentials: Path, rsa_private_pem: bytes, caplog: pytest.LogCaptureFixture
) -> None:
    script = Script().on(
        "POST",
        SEND_PATH,
        by_token(
            {"device-token-1": reply("fcm", "send_ok"), "gone": reply("fcm", "error_unregistered", 404)}
        ),
    )
    with caplog.at_level(logging.DEBUG):
        fcm(script, credentials).send([order_push(), order_push("gone")])
    for secret in ("device-token-1", "ya29.fcm-access-token-1", "BEGIN PRIVATE KEY", "kuryerga topshirildi"):
        assert secret not in caplog.text
    assert rsa_private_pem.decode().splitlines()[1] not in caplog.text


def test_the_service_account_loader_never_echoes_the_file(tmp_path: Path, credentials: Path) -> None:
    account = load_service_account(str(credentials))
    assert account.client_email == EMAIL and "BEGIN" not in repr(account)
    path = tmp_path / "sa.json"
    path.write_text('{"type": "service_account", "client_email": "x@y", "private_key": "SECRET-MATERIAL"}')
    with pytest.raises(ServiceAccountError) as refused:
        load_service_account(str(path))
    assert "SECRET-MATERIAL" not in str(refused.value)
    for content, problem in (
        ("[]", "not a service-account"),
        ("{", "not JSON"),
        ('{"type": "service_account"}', "client_email"),
    ):
        path.write_text(content)
        with pytest.raises(ServiceAccountError, match=problem):
            load_service_account(str(path))
    with pytest.raises(ServiceAccountError, match="not set"):
        load_service_account("")


@override_settings(FCM_MODE="live", FCM_PROJECT_ID="glow-derma", FCM_CREDENTIALS_FILE="/run/secrets/fcm.json")
def test_the_registry_builds_the_live_adapter_from_the_settings() -> None:
    live = get_fcm_port()
    assert isinstance(live, LiveFcm) and live.name == "fcm"
    assert (live.project_id, live.credentials_file) == ("glow-derma", "/run/secrets/fcm.json")
