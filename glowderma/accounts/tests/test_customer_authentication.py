"""Customer authentication and the customer base views (customer contract §1.1–§1.3, §1.9, §2.1.6, §6.2)."""

import threading
import uuid
from datetime import timedelta
from typing import Any

import pytest
from django.core.cache import cache
from django.db import connection
from django.test import override_settings
from django.test.utils import CaptureQueriesContext
from django.urls import path
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.test import APIClient

from glowderma.accounts import services, tokens
from glowderma.accounts.api.customer_base import (
    CustomerAPIView,
    CustomerAuthAPIView,
    PublicAPIView,
    WebhookAPIView,
)
from glowderma.accounts.models import AuthSession, Customer, Device
from glowderma.accounts.tests.factories import (
    CustomerFactory,
    device_headers,
    raw_device_key,
    start_test_session,
)
from glowderma.core import enums, timeutils
from glowderma.core.idempotency import idempotent
from glowderma.core.models import AuditEntry, IdempotencyRecord


class EchoView(CustomerAPIView):
    def get(self, request: Request) -> Response:
        device = self.device
        return Response(
            {
                "customer": request.user.pk,
                "fid": device.fid if device else None,
                "language": self.language,
            }
        )


class IdempotentEchoView(CustomerAPIView):
    @idempotent()
    def post(self, request: Request) -> Response:
        data: Any = request.data
        if data.get("refuse"):
            AuditEntry.objects.create(
                actor_type="system", action="test.refused", entity_type="t", entity_id="1"
            )
            from glowderma.core.api.exceptions import CustomerError

            raise CustomerError("conflict", 409)
        return Response({"value": data.get("value"), "customer": request.user.pk}, status=201)


class PublicEchoView(PublicAPIView):
    public_cache_seconds = 60

    def get(self, request: Request) -> Response:
        user: Any = request.user
        return Response({"customer": user.pk if isinstance(user, Customer) else None})


class NoCachePublicView(PublicAPIView):
    def get(self, request: Request) -> Response:
        return Response({})


class AuthEchoView(CustomerAuthAPIView):
    def post(self, request: Request) -> Response:
        device = self.device
        return Response({"fid": device.fid if device else None})


class HookView(WebhookAPIView):
    def post(self, request: Request) -> Response:
        return Response({"ok": True, "data": request.data})


urlpatterns = [
    path("api/v1/test/echo/", EchoView.as_view()),
    path("api/v1/test/idem/", IdempotentEchoView.as_view()),
    path("api/v1/test/public/", PublicEchoView.as_view()),
    path("api/v1/test/public-nocache/", NoCachePublicView.as_view()),
    path("api/v1/test/auth/", AuthEchoView.as_view()),
    path("api/v1/test/hook/", HookView.as_view()),
]

pytestmark = [pytest.mark.django_db, pytest.mark.urls(__name__)]


def bearer_client(token: str, **headers: Any) -> APIClient:
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {token}", **headers)
    return client


# --- audience separation
# -------------------------------------------------------------------------------------


def test_customer_client_reaches_a_customer_route(customer_client: APIClient, customer: Customer) -> None:
    response = customer_client.get("/api/v1/test/echo/", HTTP_ACCEPT_LANGUAGE="en")
    assert response.status_code == 200
    assert response.json() == {"customer": customer.pk, "fid": "fid-test-0001", "language": "en"}
    assert response["Content-Language"] == "en" and response["Cache-Control"] == "no-store"


def test_staff_token_on_a_customer_route_is_401(staff_token: str) -> None:
    response = bearer_client(staff_token, **device_headers()).get("/api/v1/test/echo/")
    assert response.status_code == 401
    assert response.json()["code"] == "not_authenticated" and response.json()["details"] is None
    assert response["WWW-Authenticate"] == 'Bearer realm="api"'


@pytest.mark.urls("config.urls")
def test_customer_token_on_an_admin_route_is_401(customer_token: str, settings: Any) -> None:
    client = bearer_client(customer_token, HTTP_X_GD_INTERNAL_SECRET=settings.INTERNAL_API_SECRET)
    response = client.get("/api/v1/admin/orders/")
    assert response.status_code == 401 and response.json()["code"] == "not_authenticated"
    assert "details" not in response.json()


def test_anonymous_and_malformed_bearers_are_401() -> None:
    assert APIClient().get("/api/v1/test/echo/", **device_headers()).status_code == 401
    assert bearer_client("not-a-jwt", **device_headers()).get("/api/v1/test/echo/").status_code == 401
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION="Bearer a b", **device_headers())
    assert client.get("/api/v1/test/echo/").status_code == 401


def test_a_token_without_rot_is_refused(customer_session: tuple[AuthSession, str]) -> None:
    import jwt
    from django.conf import settings

    session = customer_session[0]
    claims: dict[str, Any] = {
        "iss": settings.JWT_ISSUER,
        "aud": "customer",
        "sub": str(session.customer_id),
        "sid": str(session.pk),
    }
    claims |= {
        "jti": "x",
        "iat": int(timeutils.now().timestamp()),
        "exp": int(timeutils.now().timestamp()) + 60,
    }
    token = jwt.encode(claims, settings.JWT_SIGNING_KEYS[settings.JWT_ACTIVE_KID], "HS256", {"kid": "test"})
    assert bearer_client(token, **device_headers()).get("/api/v1/test/echo/").status_code == 401
    claims["rot"] = -1
    token = jwt.encode(claims, settings.JWT_SIGNING_KEYS[settings.JWT_ACTIVE_KID], "HS256", {"kid": "test"})
    assert bearer_client(token, **device_headers()).get("/api/v1/test/echo/").status_code == 401


def test_customer_access_ttl_and_rot_claim(customer_session: tuple[AuthSession, str], settings: Any) -> None:
    settings.CUSTOMER_ACCESS_TTL = 300
    token, expires = tokens.issue_access(customer_session[0])
    claims = tokens.decode_access(token, audience="customer")
    assert claims["rot"] == 0 and claims["aud"] == "customer"
    assert claims["exp"] - claims["iat"] == 300
    assert expires.timestamp() == claims["exp"]


# --- revocation through the cache
# ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("tombstone", "code"),
    [
        ("revoked:replaced", "SESSION_REPLACED"),
        ("revoked:password_changed", "password_changed"),
        ("revoked:refresh_reused", "SESSION_EXPIRED"),
        ("revoked:logout", "SESSION_EXPIRED"),
        ("revoked:deactivated", "SESSION_EXPIRED"),
        ("revoked:admin_revoked", "SESSION_EXPIRED"),
    ],
)
def test_revocation_codes_come_from_the_cache_without_a_query(
    customer_client: APIClient, customer_session: tuple[AuthSession, str], tombstone: str, code: str
) -> None:
    cache.set(f"auth:sid:{customer_session[0].pk}", tombstone, timeout=60)
    with CaptureQueriesContext(connection) as queries:
        response = customer_client.get("/api/v1/test/echo/", HTTP_ACCEPT_LANGUAGE="uz")
    assert response.status_code == 401 and response.json()["code"] == code
    assert len(queries) == 0
    if code == "password_changed":
        assert response.json()["message"].startswith("Parolingiz o‘zgartirildi.")


@pytest.mark.parametrize(
    ("reason", "code"),
    [
        ("replaced", "SESSION_REPLACED"),
        ("password_changed", "password_changed"),
        ("logout", "SESSION_EXPIRED"),
    ],
)
def test_revocation_codes_from_the_database(
    customer_client: APIClient, customer_session: tuple[AuthSession, str], reason: str, code: str
) -> None:
    AuthSession.objects.filter(pk=customer_session[0].pk).update(
        revoked_at=timeutils.now(), revoke_reason=reason
    )
    cache.delete(f"auth:sid:{customer_session[0].pk}")
    assert customer_client.get("/api/v1/test/echo/").json()["code"] == code


def test_revoke_customer_sessions_writes_tombstones_on_commit(
    customer_client: APIClient,
    customer: Customer,
    customer_session: tuple[AuthSession, str],
    django_capture_on_commit_callbacks: Any,
) -> None:
    assert customer_client.get("/api/v1/test/echo/").status_code == 200  # the active entry is cached now
    with django_capture_on_commit_callbacks(execute=True):
        assert services.revoke_customer_sessions(customer.pk, "password_changed") == 1
    assert cache.get(f"auth:sid:{customer_session[0].pk}") == "revoked:password_changed"
    assert customer_client.get("/api/v1/test/echo/").json()["code"] == "password_changed"
    with pytest.raises(ValueError, match="revoke reason"):
        services.revoke_customer_sessions(customer.pk, "bored")


def test_deleted_or_missing_customers_and_sessions_are_session_expired(
    customer_client: APIClient, customer: Customer, customer_session: tuple[AuthSession, str]
) -> None:
    Customer.objects.filter(pk=customer.pk).update(
        status=enums.CustomerStatus.DELETED, deleted_at=timeutils.now()
    )
    assert customer_client.get("/api/v1/test/echo/").json()["code"] == "SESSION_EXPIRED"
    token, _ = tokens.issue_access(customer_session[0])
    session = customer_session[0]
    session.pk = uuid.uuid4()  # a token for a session that does not exist
    ghost, _ = tokens.issue_access(session)
    assert (
        bearer_client(ghost, **device_headers()).get("/api/v1/test/echo/").json()["code"] == "SESSION_EXPIRED"
    )


def test_expired_sessions_are_refused(
    customer_client: APIClient, customer_session: tuple[AuthSession, str]
) -> None:
    AuthSession.objects.filter(pk=customer_session[0].pk).update(
        absolute_expires_at=timeutils.now() - timedelta(seconds=1)
    )
    assert customer_client.get("/api/v1/test/echo/").json()["code"] == "SESSION_EXPIRED"
    cache.set(f"auth:sid:{customer_session[0].pk}", "garbage", timeout=60)
    assert customer_client.get("/api/v1/test/echo/").json()["code"] == "SESSION_EXPIRED"


def test_a_staff_session_id_in_a_customer_token_is_refused(staff_session: tuple[AuthSession, str]) -> None:
    session = staff_session[0]
    session.audience = "customer"
    session.customer_id = session.staff_id
    token, _ = tokens.issue_access(session)
    assert (
        bearer_client(token, **device_headers()).get("/api/v1/test/echo/").json()["code"]
        == "not_authenticated"
    )


# --- device headers
# ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"HTTP_X_GD_DEVICE_ID": "fid-test-0001"},
        device_headers(fid="short"),
        device_headers(platform="windows"),
        device_headers(version="1.0"),
    ],
)
def test_missing_or_malformed_device_headers_are_400(customer_token: str, headers: dict[str, Any]) -> None:
    response = bearer_client(customer_token, **headers).get("/api/v1/test/echo/", HTTP_ACCEPT_LANGUAGE="ru")
    assert response.status_code == 400
    assert response.json() == {
        "code": "device_required",
        "message": "Не переданы данные устройства. Обновите приложение.",
        "errors": [],
        "details": None,
        "request_id": response.json()["request_id"],
    }


def test_fid_mismatch_is_401_with_one_audit_row_per_hour(
    customer_token: str, customer: Customer, customer_session: tuple[AuthSession, str]
) -> None:
    client = bearer_client(customer_token, **device_headers(fid="fid-other-0002"))
    for _ in range(3):
        assert client.get("/api/v1/test/echo/").status_code == 401
    rows = AuditEntry.objects.filter(action="auth.device_mismatch")
    assert rows.count() == 1
    row = rows.get()
    assert (row.actor_type, row.actor_id, row.after) == (
        "customer",
        customer.pk,
        {"sid": str(customer_session[0].pk)},
    )
    assert "fid-other" not in str(row.after) and "+998" not in row.actor_label.replace("+998 ", "")


def test_platform_mismatch_is_401(customer_token: str) -> None:
    response = bearer_client(customer_token, **device_headers(platform="android")).get("/api/v1/test/echo/")
    assert response.status_code == 401 and response.json()["code"] == "not_authenticated"


@override_settings(MIN_APP_VERSION_IOS="1.2.0", APP_STORE_URL="https://apps.apple.com/app/id1")
def test_app_below_the_floor_is_426_with_details(customer_token: str) -> None:
    response = bearer_client(customer_token, **device_headers(version="1.1.9")).get("/api/v1/test/echo/")
    assert response.status_code == 426
    assert response.json()["code"] == "app_update_required"
    assert response.json()["details"] == {
        "min_version": "1.2.0",
        "store_url": "https://apps.apple.com/app/id1",
    }
    assert (
        bearer_client(customer_token, **device_headers(version="1.10.0"))
        .get("/api/v1/test/echo/")
        .status_code
        != 426
    )


@override_settings(MIN_APP_VERSION_ANDROID="2.0.0", PLAY_STORE_URL="https://play.google.com/x")
def test_the_android_floor_applies_on_public_routes_too() -> None:
    response = APIClient().get("/api/v1/test/public/", **device_headers(platform="android", version="1.9.9"))
    assert (
        response.status_code == 426 and response.json()["details"]["store_url"] == "https://play.google.com/x"
    )


def test_device_bump_is_rate_limited(
    customer_client: APIClient, customer_session: tuple[AuthSession, str]
) -> None:
    device = customer_session[0].device
    assert device is not None
    Device.objects.filter(pk=device.pk).update(
        last_active_at=timeutils.now() - timedelta(days=1), app_version="0.9.0"
    )
    customer_client.get("/api/v1/test/echo/")
    device.refresh_from_db()
    assert device.app_version == "1.0.0" and device.last_active_at > timeutils.now() - timedelta(minutes=1)
    Device.objects.filter(pk=device.pk).update(app_version="0.9.0")
    customer_client.get("/api/v1/test/echo/")
    device.refresh_from_db()
    assert device.app_version == "0.9.0"  # within 5 minutes: no second write


# --- rotation confirmation (§2.1.6)
# -----------------------------------------------------------------------------


def test_the_first_request_with_the_current_rot_confirms_once(
    customer_client: APIClient, customer_session: tuple[AuthSession, str]
) -> None:
    session = customer_session[0]
    with CaptureQueriesContext(connection) as first:
        assert customer_client.get("/api/v1/test/echo/").status_code == 200
    updates = [
        q["sql"]
        for q in first.captured_queries
        if q["sql"].startswith("UPDATE") and "rotation_confirmed" in q["sql"]
    ]
    assert len(updates) == 1
    session.refresh_from_db()
    assert session.rotation_confirmed is True
    with CaptureQueriesContext(connection) as later:
        customer_client.get("/api/v1/test/echo/")
    assert not [q for q in later.captured_queries if q["sql"].startswith("UPDATE") and "rotation" in q["sql"]]


def test_an_older_rotation_never_confirms(customer_session: tuple[AuthSession, str]) -> None:
    session = customer_session[0]
    old_token, _ = tokens.issue_access(session)  # rot 0
    AuthSession.objects.filter(pk=session.pk).update(rotation=1, rotation_confirmed=False)
    assert bearer_client(old_token, **device_headers()).get("/api/v1/test/echo/").status_code == 200
    session.refresh_from_db()
    assert (session.rotation, session.rotation_confirmed) == (1, False)
    new_token, _ = tokens.issue_access(session)
    bearer_client(new_token, **device_headers()).get("/api/v1/test/echo/")
    session.refresh_from_db()
    assert session.rotation_confirmed is True


@override_settings(FAKE_NOW=timeutils.now().replace(microsecond=0))
def test_confirmation_works_under_a_pinned_clock(customer_session: tuple[AuthSession, str]) -> None:
    session = customer_session[0]
    AuthSession.objects.filter(pk=session.pk).update(
        rotation=3, rotated_at=timeutils.now(), absolute_expires_at=timeutils.now() + timedelta(days=60)
    )
    session.refresh_from_db()
    token, _ = tokens.issue_access(session)
    assert bearer_client(token, **device_headers()).get("/api/v1/test/echo/").status_code == 200
    session.refresh_from_db()
    assert session.rotation_confirmed is True


@pytest.mark.django_db(transaction=True)
def test_a_concurrent_confirmation_never_overwrites_a_revocation() -> None:
    customer = CustomerFactory.create()
    session = start_test_session(customer).session
    token, _ = tokens.issue_access(session)
    gate = threading.Event()
    results: list[int] = []

    def confirm() -> None:
        try:
            gate.wait(10)
            results.append(bearer_client(token, **device_headers()).get("/api/v1/test/echo/").status_code)
        finally:
            connection.close()

    thread = threading.Thread(target=confirm)
    thread.start()
    gate.set()
    services.revoke_customer_sessions(customer.pk, "logout")  # tombstone written on commit
    thread.join(10)
    assert results and results[0] in (200, 401)
    assert cache.get(f"auth:sid:{session.pk}") == "revoked:logout"
    response = bearer_client(token, **device_headers()).get("/api/v1/test/echo/")
    assert response.status_code == 401 and response.json()["code"] == "SESSION_EXPIRED"


# --- public, auth and webhook bases
# ------------------------------------------------------------------------------


def test_public_route_caches_anonymous_reads_only(customer_token: str) -> None:
    anonymous = APIClient().get("/api/v1/test/public/")
    assert anonymous.status_code == 200 and anonymous.json() == {"customer": None}
    assert anonymous["Cache-Control"] == "public, max-age=60" and anonymous["Vary"] == "Accept-Language"
    signed = bearer_client(customer_token, **device_headers()).get("/api/v1/test/public/")
    assert signed.json()["customer"] is not None
    assert (
        signed["Cache-Control"] == "private, no-cache" and signed["Vary"] == "Accept-Language, Authorization"
    )
    assert APIClient().get("/api/v1/test/public-nocache/")["Cache-Control"] == "no-store"


def test_public_route_with_an_invalid_bearer_is_401_never_anonymous() -> None:
    response = bearer_client("junk").get("/api/v1/test/public/")
    assert response.status_code == 401
    assert response["Cache-Control"] == "no-store"


def test_public_route_with_a_bearer_but_no_device_headers_is_401(customer_token: str) -> None:
    assert bearer_client(customer_token).get("/api/v1/test/public/").status_code == 401


def test_public_route_validates_present_device_headers() -> None:
    assert APIClient().get("/api/v1/test/public/", HTTP_X_GD_DEVICE_ID="fid-only-0001").status_code == 400


def test_auth_route_requires_device_headers_and_no_bearer() -> None:
    assert APIClient().post("/api/v1/test/auth/", {}, format="json").json()["code"] == "device_required"
    response = APIClient().post("/api/v1/test/auth/", {}, format="json", **device_headers(fid="fid-new-0009"))
    assert response.status_code == 200 and response.json() == {"fid": "fid-new-0009"}


def test_webhook_base_has_no_auth_and_no_store() -> None:
    response = APIClient().post("/api/v1/test/hook/", {}, format="json")
    assert response.status_code == 415  # parsers are declared per provider view
    assert response["Cache-Control"] == "no-store"


# --- idempotency with a customer principal (§1.7)
# --------------------------------------------------------------


def test_idempotency_replays_refuses_reuse_and_scopes_by_audience(
    customer_client: APIClient, customer: Customer
) -> None:
    key = str(uuid.uuid4())
    first = customer_client.post("/api/v1/test/idem/", {"value": 1}, format="json", HTTP_IDEMPOTENCY_KEY=key)
    replay = customer_client.post("/api/v1/test/idem/", {"value": 1}, format="json", HTTP_IDEMPOTENCY_KEY=key)
    assert first.status_code == replay.status_code == 201 and replay["Idempotent-Replayed"] == "true"
    assert replay.json() == first.json() == {"value": 1, "customer": customer.pk}
    reused = customer_client.post("/api/v1/test/idem/", {"value": 2}, format="json", HTTP_IDEMPOTENCY_KEY=key)
    assert reused.status_code == 422 and reused.json()["code"] == "idempotency_key_reused"
    assert reused.json()["details"] is None
    record = IdempotencyRecord.objects.get(key=key)
    assert (record.audience, record.actor_id) == ("customer", customer.pk)
    missing = customer_client.post("/api/v1/test/idem/", {"value": 1}, format="json")
    assert missing.status_code == 400 and missing.json()["code"] == "idempotency_key_required"


def test_a_staff_row_with_the_same_key_and_actor_id_does_not_replay(
    customer_client: APIClient, customer: Customer
) -> None:
    key = uuid.uuid4()
    IdempotencyRecord.objects.create(
        audience="staff",
        actor_id=customer.pk,
        method="POST",
        path="/api/v1/test/idem/",
        key=key,
        request_hash="x",
        response_status=201,
        response_body={"staff": True},
    )
    response = customer_client.post(
        "/api/v1/test/idem/", {"value": 3}, format="json", HTTP_IDEMPOTENCY_KEY=str(key)
    )
    assert response.status_code == 201 and response.json() == {"value": 3, "customer": customer.pk}


def test_a_409_under_idempotency_writes_nothing(customer_client: APIClient) -> None:
    key = str(uuid.uuid4())
    response = customer_client.post(
        "/api/v1/test/idem/", {"refuse": True}, format="json", HTTP_IDEMPOTENCY_KEY=key
    )
    assert response.status_code == 409 and response.json()["code"] == "conflict"
    assert not AuditEntry.objects.filter(action="test.refused").exists()  # rolled back
    assert not IdempotencyRecord.objects.filter(key=key).exists()


def test_known_device_key_header_reaches_the_service(
    customer: Customer, customer_session: tuple[AuthSession, str]
) -> None:
    device = customer_session[0].device
    assert device is not None
    assert services.device_is_known(customer.pk, device.fid, raw_device_key(device))
