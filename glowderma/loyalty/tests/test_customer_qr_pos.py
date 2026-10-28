"""``GET me/qr/`` and ``POST pos/cashback/resolve-qr/`` (customer contract §2.9.3, §2.10; plan §4 P3)."""

import hashlib
import re
import threading
import time
from collections.abc import Callable
from io import StringIO
from typing import Any

import pytest
from django.core.management import CommandError, call_command
from django.db import connection
from django.test import override_settings
from rest_framework.test import APIClient

from glowderma.accounts import services as account_services
from glowderma.accounts import sessions as account_sessions
from glowderma.accounts.models import AuthSession, Customer
from glowderma.accounts.tests.factories import CustomerFactory, StaffUserFactory, start_test_session
from glowderma.core import qr_store, timeutils
from glowderma.core.models import AuditEntry
from glowderma.core.seeding import pinned_clock
from glowderma.loyalty import qr, seeds
from glowderma.loyalty.models import LoyaltyAccount, PosDevice
from glowderma.loyalty.tests.support import at, seed_policies

QR = "/api/v1/me/qr/"
RESOLVE = "/api/v1/pos/cashback/resolve-qr/"
NOW = at(10, 26, 12, 0)

pytestmark = pytest.mark.django_db


def _token(client: APIClient) -> str:
    response = client.get(QR)
    assert response.status_code == 200, response.content
    return str(response.json()["token"])


def _resolve(client: APIClient, token: str, sale_ref: str | None = None) -> Any:
    return client.post(RESOLVE, {"token": token, "sale_ref": sale_ref}, format="json")


# --- QR issue ---------------------------------------------------------------------------------------------


def test_qr_token_format_and_lifetime(customer_client: APIClient) -> None:
    with pinned_clock(NOW):
        response = customer_client.get(QR)
    assert response.status_code == 200
    assert response["Cache-Control"] == "no-store"
    body = response.json()
    assert set(body) == {"token", "expires_at", "ttl_seconds", "refresh_in_seconds"}
    assert qr.TOKEN_PATTERN.fullmatch(body["token"]) and len(body["token"]) == 26
    assert body["expires_at"] == "2026-10-26T12:01:00+05:00"
    assert (body["ttl_seconds"], body["refresh_in_seconds"]) == (60, 50)


def test_qr_raw_token_is_not_stored_and_not_logged(
    customer: Customer, customer_client: APIClient, caplog: pytest.LogCaptureFixture
) -> None:
    token = _token(customer_client)
    # only the HMAC of the token is a key; the stored value is "{customer_id}:{epoch}:{session_id}"
    assert qr_store._local.values  # LocMem stand-in under the test settings
    for key, (value, _expiry) in qr_store._local.values.items():
        assert token not in key and token not in value
        assert value.startswith(f"{customer.pk}:")
    assert qr_store.token_key(qr.key_hash(token)) in qr_store._local.values
    assert token not in caplog.text
    assert not AuditEntry.objects.exists()  # no audit on issue


def test_qr_expires_after_the_ttl(
    customer_client: APIClient, pos_client: APIClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    token = _token(customer_client)
    real = time.monotonic()
    monkeypatch.setattr(time, "monotonic", lambda: real + 61)
    response = _resolve(pos_client, token)
    assert response.status_code == 409 and response.json()["code"] == "qr_invalid"


def test_a_third_token_evicts_the_first(customer_client: APIClient, pos_client: APIClient) -> None:
    first, second, third = (_token(customer_client) for _ in range(3))
    assert _resolve(pos_client, first).status_code == 409
    assert _resolve(pos_client, second).status_code == 200
    assert _resolve(pos_client, third).status_code == 200


def test_qr_throttle(customer_client: APIClient) -> None:
    statuses = [customer_client.get(QR).status_code for _ in range(7)]
    assert statuses == [200] * 6 + [429]
    throttled = customer_client.get(QR)
    assert throttled.json()["code"] == "throttled" and throttled.has_header("Retry-After")


@pytest.mark.parametrize("how", ["logout", "replaced", "session_revoked", "deleted"])
def test_tokens_are_revoked_on_logout_replace_and_delete(
    how: str,
    customer: Customer,
    customer_session: tuple[AuthSession, str],
    customer_client: APIClient,
    pos_client: APIClient,
    django_capture_on_commit_callbacks: Callable[..., Any],
) -> None:
    token = _token(customer_client)
    with django_capture_on_commit_callbacks(execute=True):
        if how == "logout":
            account_services.revoke_customer_sessions(customer.pk, "logout")
        elif how == "replaced":
            start_test_session(customer)  # the same install signs in again: its old session ends
        elif how == "session_revoked":
            account_sessions.revoke_session(
                customer.pk, str(customer_session[0].pk), actor=customer, actor_type="customer"
            )
        else:
            account_services.delete_customer(customer.pk)
    response = _resolve(pos_client, token)
    assert response.status_code == 409 and response.json()["code"] == "qr_invalid"


def test_a_sign_in_on_another_device_keeps_the_token(
    customer: Customer,
    customer_client: APIClient,
    pos_client: APIClient,
    django_capture_on_commit_callbacks: Callable[..., Any],
) -> None:
    """B-W1: the phone showing the QR stays signed in while the customer signs in on the website."""
    token = _token(customer_client)
    with django_capture_on_commit_callbacks(execute=True):
        start_test_session(customer, fid="fid-website-01", platform="web", app_version="")
    assert _resolve(pos_client, token).status_code == 200


def test_a_token_of_an_ended_session_is_refused_even_if_its_key_survived(
    customer: Customer,
    customer_session: tuple[AuthSession, str],
    customer_client: APIClient,
    pos_client: APIClient,
) -> None:
    """The resolve re-checks the issuing session: a revocation whose on-commit key deletion was lost (Redis
    down at that moment) still makes the token worthless."""
    token = _token(customer_client)
    AuthSession.objects.filter(pk=customer_session[0].pk).update(
        revoked_at=timeutils.now(), revoke_reason="logout"
    )
    response = _resolve(pos_client, token)
    assert response.status_code == 409 and response.json()["code"] == "qr_invalid"


# --- POS resolve ------------------------------------------------------------------------------------------


def test_resolve_answers_the_allow_listed_fields_only(
    customer: Customer, customer_client: APIClient, pos_client: APIClient, pos_device: PosDevice
) -> None:
    seed_policies()
    LoyaltyAccount.objects.create(customer=customer, balance=48_600)
    Customer.objects.filter(pk=customer.pk).update(name="Malika Karimova", phone="+998901234567")
    token = _token(customer_client)
    with pinned_clock(NOW):
        response = _resolve(pos_client, token, sale_ref="CHK-1001")
    assert response.status_code == 200
    body = response.json()
    assert body == {
        "customer_ref": qr.pos_customer_ref(pos_device.pk, customer.pk),
        "phone": "+998901234567",
        "phone_mask": "+998 90 *** ** 67",
        "available": 48_600,
        "percent": "1.00",
        "resolved_at": "2026-10-26T12:00:00+05:00",
    }
    text = response.content.decode()
    assert "Malika" not in text and "customer_id" not in text
    assert body["customer_ref"].startswith("c_") and len(body["customer_ref"]) == 18
    # stable per (device, customer), different for another device
    assert body["customer_ref"] == qr.pos_customer_ref(pos_device.pk, customer.pk)
    assert qr.pos_customer_ref(pos_device.pk + 1, customer.pk) != body["customer_ref"]
    entry = AuditEntry.objects.get(action="pos.qr_resolved")
    assert (entry.actor_type, entry.actor_id, entry.actor_label) == (
        "integration",
        pos_device.pk,
        "Kassa test",
    )
    assert entry.after == {"pos_device_id": pos_device.pk, "customer_id": customer.pk, "sale_ref": "CHK-1001"}
    pos_device.refresh_from_db()
    assert pos_device.last_used_at is not None


def test_resolve_is_single_use(customer_client: APIClient, pos_client: APIClient) -> None:
    token = _token(customer_client)
    assert _resolve(pos_client, token).status_code == 200
    second = _resolve(pos_client, token)
    assert second.status_code == 409
    assert second.json() == {
        "code": "qr_invalid",
        "message": ("QR kod yaroqsiz yoki muddati o‘tgan. Mijozdan ilovada QR kodni yangilashni so‘rang."),
        "errors": [],
        "details": None,
        "request_id": second.json()["request_id"],
    }


@pytest.mark.parametrize("token", ["GD1:short", "XX1:3q2-7wEjRZ8nYlZTS3l6qg", "GD1:3q2-7wEjRZ8nYlZTS3l6q!"])
def test_malformed_token_is_rejected_with_the_same_body(
    token: str, pos_client: APIClient, pos_device: PosDevice
) -> None:
    response = _resolve(pos_client, token)
    assert response.status_code == 409 and response.json()["code"] == "qr_invalid"
    entry = AuditEntry.objects.get(action="pos.qr_rejected")
    assert entry.after == {"pos_device_id": pos_device.pk, "reason": "format"}


def test_unknown_token_and_deleted_customer(
    customer: Customer, customer_client: APIClient, pos_client: APIClient
) -> None:
    assert _resolve(pos_client, qr.new_token()).status_code == 409
    token = _token(customer_client)
    Customer.objects.filter(pk=customer.pk).update(status="deleted", deleted_at=NOW)
    response = _resolve(pos_client, token)
    assert response.status_code == 409 and response.json()["code"] == "qr_invalid"
    reasons = list(
        AuditEntry.objects.filter(action="pos.qr_rejected").values_list("after__reason", flat=True)
    )
    assert reasons == ["unknown", "unknown"]


def test_resolve_body_validation(pos_client: APIClient) -> None:
    response = pos_client.post(RESOLVE, {}, format="json")
    assert response.status_code == 400 and response.json()["errors"][0]["field"] == "token"
    assert pos_client.post(RESOLVE, {"token": "x" * 65}, format="json").status_code == 400


def test_resolve_messages_follow_accept_language(pos_client: APIClient) -> None:
    response = pos_client.post(RESOLVE, {"token": "bad"}, format="json", HTTP_ACCEPT_LANGUAGE="ru")
    assert response.json()["message"].startswith("QR-код недействителен")
    assert response["Content-Language"] == "ru"


# --- POS authentication -----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "authorization",
    [None, "Api-Key gdpos_unknown0_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx", "Bearer something", "Api-Key"],
)
def test_missing_or_unknown_key_is_401(authorization: str | None, db: None) -> None:
    client = APIClient()
    if authorization:
        client.credentials(HTTP_AUTHORIZATION=authorization)
    response = client.post(RESOLVE, {"token": qr.new_token()}, format="json")
    assert response.status_code == 401
    assert response["WWW-Authenticate"] == "Api-Key"
    assert response.json()["code"] == "not_authenticated"


def test_inactive_key_is_401(pos_client: APIClient, pos_device: PosDevice) -> None:
    PosDevice.objects.filter(pk=pos_device.pk).update(is_active=False)
    assert pos_client.post(RESOLVE, {"token": qr.new_token()}, format="json").status_code == 401


def test_customer_token_on_pos_is_401(customer_token: str) -> None:
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {customer_token}")
    assert client.post(RESOLVE, {"token": qr.new_token()}, format="json").status_code == 401


def test_the_21st_failed_auth_from_one_ip_is_429_without_a_lookup(
    pos_client: APIClient, pos_key: str, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    bad = APIClient()
    bad.credentials(HTTP_AUTHORIZATION="Api-Key gdpos_wrong000_yyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyy")
    for _ in range(20):
        assert bad.post(RESOLVE, {"token": "x"}, format="json").status_code == 401
    lookups: list[str] = []
    from glowderma.loyalty import authentication

    original = authentication.authenticate_pos_key

    def spy(raw: str) -> PosDevice | None:
        lookups.append(raw)
        return original(raw)

    monkeypatch.setattr(authentication, "authenticate_pos_key", spy)
    blocked = bad.post(RESOLVE, {"token": "x"}, format="json")
    assert blocked.status_code == 429 and blocked.json()["code"] == "throttled"
    assert blocked.has_header("Retry-After")
    # even a valid key from that IP waits out the window, and no lookup happens
    assert pos_client.post(RESOLVE, {"token": "x"}, format="json").status_code == 429
    assert lookups == []
    assert pos_key not in caplog.text and "gdpos_wrong000" not in caplog.text


@override_settings(THROTTLE_RATES={"pos": "2/min"})
def test_pos_throttle_per_device(pos_client: APIClient) -> None:
    statuses = [pos_client.post(RESOLVE, {"token": "bad"}, format="json").status_code for _ in range(3)]
    assert statuses == [409, 409, 429]


# --- POS key commands -------------------------------------------------------------------------------------


def _run(name: str, *args: str) -> str:
    out = StringIO()
    call_command(name, *args, stdout=out)
    return out.getvalue()


def test_createposkey_prints_the_key_once_and_stores_only_the_hash(
    customer_client: APIClient,
) -> None:
    staff = StaffUserFactory.create(email="kassir@glowderma.uz")
    output = _run("createposkey", "--name", "Kassa 1", "--location", "Yunusobod", "--actor", staff.email)
    raw_key = output.strip().splitlines()[-1]
    assert re.fullmatch(r"gdpos_[a-z0-9]{8}_[A-Za-z0-9_-]{32}", raw_key)
    parts = raw_key.split("_")
    assert parts[0] == "gdpos" and len(parts[1]) == 8 and len(raw_key) == len("gdpos_") + 8 + 1 + 32
    device = PosDevice.objects.get(name="Kassa 1")
    assert device.key_hash == hashlib.sha256(raw_key.encode()).hexdigest()
    assert raw_key not in device.key_hash and device.key_prefix == parts[1]
    assert (device.location_label, device.created_by_id, device.is_active) == ("Yunusobod", staff.pk, True)
    entry = AuditEntry.objects.get(action="pos.device_created")
    assert (entry.actor_type, entry.actor_id) == ("staff", staff.pk)
    assert entry.after == {"pos_device_id": device.pk, "name": "Kassa 1"}
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f"Api-Key {raw_key}")
    token = _token(customer_client)
    assert _resolve(client, token).status_code == 200


def test_createposkey_requires_an_active_staff_actor(db: None) -> None:
    with pytest.raises(CommandError):
        _run("createposkey", "--name", "Kassa 1")
    with pytest.raises(CommandError, match="active staff"):
        _run("createposkey", "--name", "Kassa 1", "--actor", "nobody@glowderma.uz")
    inactive = StaffUserFactory.create(is_active=False)
    with pytest.raises(CommandError, match="active staff"):
        _run("createposkey", "--name", "Kassa 1", "--actor", inactive.email)
    assert not PosDevice.objects.exists()


def test_revokeposkey_then_401(pos_client: APIClient, pos_device: PosDevice) -> None:
    staff = StaffUserFactory.create()
    _run("revokeposkey", pos_device.key_prefix, "--actor", staff.email)
    assert pos_client.post(RESOLVE, {"token": "bad"}, format="json").status_code == 401
    entry = AuditEntry.objects.get(action="pos.device_revoked")
    assert entry.actor_id == staff.pk and entry.after == {"pos_device_id": pos_device.pk}
    with pytest.raises(CommandError):
        _run("revokeposkey", "nosuch00", "--actor", staff.email)


def test_rotateposkey_old_key_401_new_key_200(
    customer_client: APIClient, pos_client: APIClient, pos_device: PosDevice
) -> None:
    staff = StaffUserFactory.create()
    raw_key = _run("rotateposkey", pos_device.key_prefix, "--actor", staff.email).strip().splitlines()[-1]
    assert pos_client.post(RESOLVE, {"token": "bad"}, format="json").status_code == 401
    fresh = APIClient()
    fresh.credentials(HTTP_AUTHORIZATION=f"Api-Key {raw_key}")
    assert _resolve(fresh, _token(customer_client)).status_code == 200
    assert raw_key.split("_")[1] == pos_device.key_prefix
    assert AuditEntry.objects.filter(action="pos.device_rotated", actor_id=staff.pk).count() == 1
    PosDevice.objects.filter(pk=pos_device.pk).update(is_active=False)
    with pytest.raises(CommandError, match="revoked"):
        _run("rotateposkey", pos_device.key_prefix, "--actor", staff.email)


def test_pos_api_keys_are_seeded_only_with_dev_features(db: None) -> None:
    key = "gdpos_demo0001_local-dev-only-pos-key-000000000"
    with override_settings(POS_API_KEYS=[("Kassa 1", key)], DEV_FEATURES_ALLOWED=False):
        assert seeds.seed_pos_devices() == 0
    assert not PosDevice.objects.exists()
    with override_settings(POS_API_KEYS=[("Kassa 1", key)]):
        assert seeds.seed_pos_devices() == 1
        assert seeds.seed_pos_devices() == 0  # idempotent
    device = PosDevice.objects.get()
    assert (device.name, device.key_prefix) == ("Kassa 1", "demo0001")
    assert device.key_hash == hashlib.sha256(key.encode()).hexdigest()


# --- races ------------------------------------------------------------------------------------------------


def test_resolve_qr_is_single_use_under_two_threads(db: None) -> None:
    customer = CustomerFactory.create()
    token = qr.issue_qr(customer.pk, "sid-race").token
    start = threading.Event()
    results: list[int | None] = []
    lock = threading.Lock()

    def scan() -> None:
        start.wait(10)
        value = qr.resolve_qr(token)
        with lock:
            results.append(value.customer_id if value is not None else None)

    threads = [threading.Thread(target=scan) for _ in range(2)]
    for thread in threads:
        thread.start()
    start.set()
    for thread in threads:
        thread.join(10)
    assert sorted(results, key=lambda v: v is None) == [customer.pk, None]


@pytest.mark.django_db(transaction=True)
def test_double_resolve_race_through_the_api(pos_key: str) -> None:
    customer = CustomerFactory.create()
    PosDevice.objects.create(
        name="Kassa race",
        key_prefix=pos_key.split("_")[1],
        key_hash=hashlib.sha256(pos_key.encode()).hexdigest(),
    )
    token = qr.issue_qr(customer.pk).token
    start = threading.Event()
    statuses: list[int] = []
    lock = threading.Lock()

    def scan() -> None:
        try:
            client = APIClient()
            client.credentials(HTTP_AUTHORIZATION=f"Api-Key {pos_key}")
            start.wait(10)
            status = client.post(RESOLVE, {"token": token}, format="json").status_code
            with lock:
                statuses.append(status)
        finally:
            connection.close()

    threads = [threading.Thread(target=scan) for _ in range(2)]
    for thread in threads:
        thread.start()
    start.set()
    for thread in threads:
        thread.join(10)
    assert sorted(statuses) == [200, 409]
