"""B-W1: the website as a client (``X-GD-Platform: web``), several sessions at once, the «Faol seanslar» list
(``me/sessions/``), signing sessions out (one, the others, everywhere) and the admin view of them."""

# ruff: noqa: S105, S106 - FCM tokens in fixtures are test data, not secrets

from typing import Any

import maxminddb
import pytest
from django.conf import settings
from django.core.cache import cache
from rest_framework.test import APIClient

from glowderma.accounts import sessions as account_sessions
from glowderma.accounts import tokens
from glowderma.accounts.models import AuthSession, Customer, Device
from glowderma.accounts.tests.factories import (
    CustomerFactory,
    device_headers,
    raw_device_key,
    start_test_session,
)
from glowderma.core import geoip, qr_store, timeutils
from glowderma.core.models import AuditEntry
from glowderma.customers.tests.p1_support import PASSWORD, TERMS, publish_terms, verification

pytestmark = pytest.mark.django_db

SESSIONS = "/api/v1/me/sessions/"
LOGOUT = "/api/v1/auth/logout/"
CHROME_WINDOWS = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/130.0.0.0 Safari/537.36"
)
WEB_FID = "web-browser-cookie-0001"


class Reader:
    def get(self, ip: str) -> Any:
        return {
            "84.54.70.10": {
                "city": {"names": {"en": "Tashkent"}},
                "subdivisions": [{"names": {"en": "Tashkent"}}],
                "country": {"iso_code": "UZ"},
            },
            "185.139.137.1": {"city": {"names": {"en": "Samarkand"}}, "country": {"iso_code": "UZ"}},
        }.get(ip)

    def close(self) -> None:
        return None


@pytest.fixture(autouse=True)
def geo(settings: Any, monkeypatch: pytest.MonkeyPatch) -> Any:
    geoip.reset()
    settings.GEOIP_CITY_DB = "/srv/geoip/city.mmdb"
    monkeypatch.setattr(maxminddb, "open_database", lambda path: Reader())
    yield
    geoip.reset()


def client_for(
    started: Any, *, platform: str = "ios", version: str = "1.0.0", bff: bool = False
) -> APIClient:
    session = started.session
    access, _ = tokens.issue_access(session)
    client = APIClient()
    client.default_format = "json"
    extra: dict[str, Any] = {}
    if bff:
        extra = {
            "HTTP_X_GD_INTERNAL_SECRET": settings.INTERNAL_API_SECRET,
            "HTTP_X_GD_CLIENT_IP": "84.54.70.10",
            "HTTP_X_GD_CLIENT_UA": CHROME_WINDOWS,
        }
    client.credentials(
        HTTP_AUTHORIZATION=f"Bearer {access}",
        **device_headers(fid=session.device.fid, platform=platform, version=version),
        **extra,
    )
    return client


def web_headers(**extra: Any) -> dict[str, Any]:
    headers = {"HTTP_X_GD_DEVICE_ID": WEB_FID, "HTTP_X_GD_PLATFORM": "web"}
    headers.update(extra)
    return headers


# --- the web platform -------------------------------------------------------------------------------------


def test_web_needs_no_app_version_and_has_no_floor(settings: Any) -> None:
    settings.MIN_APP_VERSION_IOS = "9.0.0"
    client = APIClient()
    response = client.get("/api/v1/catalog/categories/", **web_headers())
    assert response.status_code == 200
    response = client.get("/api/v1/catalog/categories/", **web_headers(HTTP_X_GD_APP_VERSION="0.0.1"))
    assert response.status_code == 200
    bad = client.get("/api/v1/catalog/categories/", **web_headers(HTTP_X_GD_APP_VERSION="next"))
    assert bad.status_code == 400 and bad.json()["code"] == "device_required"
    app = client.get(
        "/api/v1/catalog/categories/",
        HTTP_X_GD_DEVICE_ID="fid-app-000001",
        HTTP_X_GD_PLATFORM="ios",
    )
    assert app.status_code == 400 and app.json()["code"] == "device_required"  # the app still needs it
    old = client.get("/api/v1/catalog/categories/", **device_headers(fid="fid-app-000001", version="1.0.0"))
    assert old.status_code == 426


def test_a_website_session_and_the_app_session_live_together() -> None:
    customer = CustomerFactory.create()
    app = start_test_session(customer)
    web = start_test_session(customer, fid=WEB_FID, platform="web", app_version="")
    app_client = client_for(app)
    web_client = client_for(web, platform="web", version="")
    web_client.credentials(
        HTTP_AUTHORIZATION=f"Bearer {tokens.issue_access(web.session)[0]}",
        **web_headers(),
    )
    assert app_client.get("/api/v1/me/").status_code == 200
    assert web_client.get("/api/v1/me/").status_code == 200
    assert Device.objects.get(fid=WEB_FID).platform == "web"


# --- the session list ---------------------------------------------------------------------------------------


def test_the_session_list_names_devices_places_and_the_current_one() -> None:
    customer = CustomerFactory.create()
    app = start_test_session(customer)
    Device.objects.filter(sessions=app.session).update(model="iPhone 15", os="iOS 18.1")
    account_sessions.stamp_session(app.session.pk, user_agent="okhttp/4.12", ip="185.139.137.1")
    web = start_test_session(customer, fid=WEB_FID, platform="web", app_version="")
    account_sessions.stamp_session(web.session.pk, user_agent=CHROME_WINDOWS, ip="84.54.70.10")
    old = timeutils.now().replace(year=2025)
    AuthSession.objects.filter(pk=app.session.pk).update(last_used_at=old)
    stranger = start_test_session(CustomerFactory.create(), fid="fid-stranger-01")
    assert stranger.session.pk

    response = client_for(app).get(SESSIONS, HTTP_ACCEPT_LANGUAGE="uz")
    assert response.status_code == 200
    body = response.json()
    assert body["count"] == 2
    first, second = body["results"]
    assert first == {
        "id": str(web.session.pk),
        "device": "Chrome (Windows)",
        "platform": "web",
        "location": "Tashkent, Tashkent, UZ",
        "created_at": timeutils.isoformat(web.session.created_at),
        "updated_at": timeutils.isoformat(web.session.last_used_at),
        "current": False,
    }
    assert second["id"] == str(app.session.pk) and second["current"] is True
    assert second["device"] == "Glow Derma iOS (iPhone 15)" and second["location"] == "Samarkand, UZ"
    assert second["created_at"] == timeutils.isoformat(app.session.created_at)


def test_unknown_place_and_browser_read_as_placeholders(settings: Any) -> None:
    geoip.reset()
    settings.GEOIP_CITY_DB = ""
    customer = CustomerFactory.create()
    web = start_test_session(customer, fid=WEB_FID, platform="web", app_version="")
    account_sessions.stamp_session(web.session.pk, user_agent="", ip="84.54.70.10")
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {tokens.issue_access(web.session)[0]}", **web_headers())
    [row] = client.get(SESSIONS, HTTP_ACCEPT_LANGUAGE="en").json()["results"]
    assert (row["device"], row["location"], row["current"]) == ("Web browser", "—", True)


def test_sign_in_records_the_browser_the_bff_forwards(settings: Any) -> None:
    """``X-GD-Client-UA`` / ``X-GD-Client-IP`` count only with the internal secret."""
    customer = CustomerFactory.create()
    started = start_test_session(customer, fid=WEB_FID, platform="web", app_version="")
    request_meta = {
        "HTTP_X_GD_INTERNAL_SECRET": settings.INTERNAL_API_SECRET,
        "HTTP_X_GD_CLIENT_UA": CHROME_WINDOWS,
        "HTTP_X_GD_CLIENT_IP": "84.54.70.10",
        "HTTP_USER_AGENT": "node",
    }

    class Request:
        META = request_meta
        client_ip = "84.54.70.10"

    user_agent, ip = account_sessions.client_facts(Request())
    account_sessions.stamp_session(started.session.pk, user_agent=user_agent, ip=ip)
    started.session.refresh_from_db()
    assert started.session.user_agent == CHROME_WINDOWS
    assert (started.session.last_ip, started.session.location) == ("84.54.70.10", "Tashkent, Tashkent, UZ")


def test_activity_is_recorded_at_most_every_five_minutes_and_follows_the_ip(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    customer = CustomerFactory.create()
    web = start_test_session(customer, fid=WEB_FID, platform="web", app_version="")
    access = tokens.issue_access(web.session)[0]
    client = APIClient()
    client.credentials(
        HTTP_AUTHORIZATION=f"Bearer {access}",
        HTTP_X_GD_INTERNAL_SECRET=settings.INTERNAL_API_SECRET,
        HTTP_X_GD_CLIENT_IP="84.54.70.10",
        HTTP_X_GD_CLIENT_UA=CHROME_WINDOWS,
        **web_headers(),
    )
    lookups: list[str] = []
    real = geoip.locate

    def counting(ip: str, language: str = "en") -> str:
        lookups.append(ip)
        return real(ip, language)

    monkeypatch.setattr(geoip, "locate", counting)
    assert client.get("/api/v1/me/").status_code == 200
    web.session.refresh_from_db()
    assert web.session.user_agent == CHROME_WINDOWS
    assert web.session.location == "Tashkent, Tashkent, UZ" and lookups == ["84.54.70.10"]
    first_seen = web.session.last_used_at
    client.credentials(
        HTTP_AUTHORIZATION=f"Bearer {access}",
        HTTP_X_GD_INTERNAL_SECRET=settings.INTERNAL_API_SECRET,
        HTTP_X_GD_CLIENT_IP="185.139.137.1",
        **web_headers(),
    )
    assert client.get("/api/v1/me/").status_code == 200  # within five minutes: nothing written
    web.session.refresh_from_db()
    assert web.session.last_used_at == first_seen and web.session.last_ip == "84.54.70.10"
    cache.delete(f"auth:devbump:{web.session.pk}")
    assert client.get("/api/v1/me/").status_code == 200
    web.session.refresh_from_db()
    assert web.session.last_ip == "185.139.137.1" and web.session.location == "Samarkand, UZ"
    assert web.session.user_agent == CHROME_WINDOWS  # no forwarded UA this time: the BFF's own "" is ignored
    assert lookups == ["84.54.70.10", "185.139.137.1"]


def test_sign_in_through_the_api_stamps_the_session(settings: Any) -> None:
    publish_terms()
    client = APIClient()
    client.credentials(
        HTTP_X_GD_INTERNAL_SECRET=settings.INTERNAL_API_SECRET,
        HTTP_X_GD_CLIENT_IP="84.54.70.10",
        HTTP_X_GD_CLIENT_UA=CHROME_WINDOWS,
        **web_headers(),
    )
    response = client.post(
        "/api/v1/auth/set-password/",
        {
            "verification_token": verification("+998901234500", "register"),
            "password": PASSWORD,
            "name": "Malika",
            "terms_version": TERMS,
            "language": "uz",
        },
        format="json",
    )
    assert response.status_code == 201, response.json()
    session = AuthSession.objects.get(customer_id=response.json()["customer"]["id"])
    assert session.device is not None and session.device.platform == "web"
    assert (session.user_agent, session.location) == (CHROME_WINDOWS, "Tashkent, Tashkent, UZ")


# --- signing sessions out -----------------------------------------------------------------------------------


def test_delete_one_session_signs_that_device_out(django_capture_on_commit_callbacks: Any) -> None:
    customer = CustomerFactory.create()
    app = start_test_session(customer)
    web = start_test_session(customer, fid=WEB_FID, platform="web", app_version="")
    Device.objects.filter(sessions=web.session).update(fcm_token="tok-web", push_enabled=True)
    qr_store.put("w" * 64, customer.pk, ttl=60, max_live=2, session_id=str(web.session.pk))
    with django_capture_on_commit_callbacks(execute=True):
        response = client_for(app).delete(f"{SESSIONS}{web.session.pk}/")
    assert response.status_code == 204
    web.session.refresh_from_db()
    assert web.session.revoke_reason == "logout"
    assert cache.get(f"auth:sid:{web.session.pk}") == "revoked:logout"
    assert qr_store.take("w" * 64) is None
    device = Device.objects.get(sessions=web.session)
    assert (device.is_active, device.push_enabled, device.fcm_token) == (False, False, "")
    app.session.refresh_from_db()
    assert app.session.revoked_at is None
    entry = AuditEntry.objects.get(action="customer.session_revoked")
    assert entry.actor_type == "customer" and entry.after == {"sid": str(web.session.pk), "current": False}
    # the revoked browser is signed out on its next request
    web_client = APIClient()
    web_client.credentials(
        HTTP_AUTHORIZATION=f"Bearer {tokens.issue_access(web.session)[0]}", **web_headers()
    )
    gone = web_client.get("/api/v1/me/")
    assert gone.status_code == 401 and gone.json()["code"] == "SESSION_EXPIRED"
    assert client_for(app).delete(f"{SESSIONS}{web.session.pk}/").status_code == 404  # already ended


def test_another_customers_session_or_a_bad_id_is_not_found() -> None:
    customer = CustomerFactory.create()
    app = start_test_session(customer)
    other = start_test_session(CustomerFactory.create(), fid="fid-other-0001")
    client = client_for(app)
    assert client.delete(f"{SESSIONS}{other.session.pk}/").status_code == 404
    assert client.delete(f"{SESSIONS}not-a-uuid/").status_code == 404
    other.session.refresh_from_db()
    assert other.session.revoked_at is None


def test_deleting_the_current_session_is_a_logout() -> None:
    customer = CustomerFactory.create()
    app = start_test_session(customer)
    client = client_for(app)
    assert client.delete(f"{SESSIONS}{app.session.pk}/").status_code == 204
    entry = AuditEntry.objects.get(action="customer.session_revoked")
    assert entry.after == {"sid": str(app.session.pk), "current": True}
    assert client.get("/api/v1/me/").json()["code"] == "SESSION_EXPIRED"


def test_revoke_others_keeps_only_the_caller(django_capture_on_commit_callbacks: Any) -> None:
    customer = CustomerFactory.create()
    app = start_test_session(customer)
    web = start_test_session(customer, fid=WEB_FID, platform="web", app_version="")
    phone = start_test_session(customer, fid="fid-android-001", platform="android")
    Device.objects.filter(sessions=phone.session).update(fcm_token="tok-android", push_enabled=True)
    with django_capture_on_commit_callbacks(execute=True):
        response = client_for(app).post(f"{SESSIONS}revoke-others/")
    assert response.status_code == 200 and response.json() == {"revoked": 2}
    open_ids = set(
        AuthSession.objects.filter(customer=customer, revoked_at__isnull=True).values_list("pk", flat=True)
    )
    assert open_ids == {app.session.pk}
    assert set(Device.objects.filter(customer=customer, is_active=True).values_list("fid", flat=True)) == {
        "fid-test-0001"
    }
    assert not Device.objects.filter(fcm_token="tok-android").exists()
    entry = AuditEntry.objects.get(action="customer.sessions_revoked")
    assert entry.after == {"count": 2, "scope": "others"}
    assert client_for(app).post(f"{SESSIONS}revoke-others/").json() == {"revoked": 0}
    assert web.session.pk not in open_ids


@pytest.mark.parametrize(
    ("scope", "left"),
    [("current", {"web", "android"}), ("others", {"ios"}), ("all", set())],
)
def test_logout_scopes(scope: str, left: set[str], django_capture_on_commit_callbacks: Any) -> None:
    customer = CustomerFactory.create()
    app = start_test_session(customer)
    start_test_session(customer, fid=WEB_FID, platform="web", app_version="")
    start_test_session(customer, fid="fid-android-001", platform="android")
    qr_store.put("l" * 64, customer.pk, ttl=60, max_live=2, session_id=str(app.session.pk))
    client = APIClient()
    client.credentials(**device_headers(key=raw_device_key(Device.objects.get(sessions=app.session))))
    from glowderma.accounts.tests.factories import raw_refresh  # noqa: PLC0415

    with django_capture_on_commit_callbacks(execute=True):
        response = client.post(LOGOUT, {"refresh": raw_refresh(app.session), "scope": scope}, format="json")
    assert response.status_code == 204
    still_open = set(
        AuthSession.objects.filter(customer=customer, revoked_at__isnull=True).values_list(
            "device__platform", flat=True
        )
    )
    assert still_open == left
    active = set(Device.objects.filter(customer=customer, is_active=True).values_list("platform", flat=True))
    assert active == left
    assert (qr_store.take("l" * 64) is None) is (scope != "others")
    entry = AuditEntry.objects.get(action="customer.logout")
    assert entry.after == {"sid": str(app.session.pk), "scope": scope}


def test_logout_without_a_scope_is_current_and_an_unknown_scope_is_refused() -> None:
    customer = CustomerFactory.create()
    app = start_test_session(customer)
    from glowderma.accounts.tests.factories import raw_refresh  # noqa: PLC0415

    client = APIClient()
    client.credentials(**device_headers())
    bad = client.post(LOGOUT, {"refresh": raw_refresh(app.session), "scope": "galaxy"}, format="json")
    assert bad.status_code == 400 and bad.json()["errors"][0]["field"] == "scope"
    assert client.post(LOGOUT, {"refresh": raw_refresh(app.session)}, format="json").status_code == 204


def test_session_management_needs_a_customer_session(anon_client: APIClient) -> None:
    assert anon_client.get(SESSIONS, **device_headers()).status_code == 401
    assert anon_client.post(f"{SESSIONS}revoke-others/", **device_headers()).status_code == 401


# --- admin --------------------------------------------------------------------------------------------------


def test_admin_lists_and_revokes_a_customers_sessions(
    staff_client: APIClient, django_capture_on_commit_callbacks: Any
) -> None:
    customer = CustomerFactory.create()
    app = start_test_session(customer)
    web = start_test_session(customer, fid=WEB_FID, platform="web", app_version="")
    account_sessions.stamp_session(web.session.pk, user_agent=CHROME_WINDOWS, ip="84.54.70.10")
    url = f"/api/v1/admin/customers/{customer.pk}/sessions/"
    response = staff_client.get(url)
    assert response.status_code == 200
    rows = response.json()["results"]
    assert [row["id"] for row in rows] == [str(web.session.pk), str(app.session.pk)]
    assert rows[0]["device"] == "Chrome (Windows)" and "current" not in rows[0]
    assert rows[1]["device"] == "Glow Derma iOS"
    with django_capture_on_commit_callbacks(execute=True):
        assert staff_client.delete(f"{url}{web.session.pk}/").status_code == 204
    web.session.refresh_from_db()
    assert web.session.revoke_reason == "admin_revoked"
    entry = AuditEntry.objects.get(action="customer.session_revoked")
    assert entry.actor_type == "staff" and entry.entity_id == str(customer.pk)
    assert staff_client.delete(f"{url}{web.session.pk}/").status_code == 404
    assert staff_client.get("/api/v1/admin/customers/999999/sessions/").status_code == 404
    assert (
        staff_client.delete(f"/api/v1/admin/customers/999999/sessions/{app.session.pk}/").status_code == 404
    )
    other = CustomerFactory.create()
    assert (
        staff_client.delete(f"/api/v1/admin/customers/{other.pk}/sessions/{app.session.pk}/").status_code
        == 404
    )


def test_the_session_of_a_deleted_customer_is_gone_from_the_list(staff_client: APIClient) -> None:
    customer = CustomerFactory.create()
    start_test_session(customer)
    from glowderma.accounts import services  # noqa: PLC0415

    services.delete_customer(customer.pk)
    assert Customer.objects.get(pk=customer.pk).status == "deleted"
    response = staff_client.get(f"/api/v1/admin/customers/{customer.pk}/sessions/")
    assert response.status_code == 200 and response.json()["count"] == 0
