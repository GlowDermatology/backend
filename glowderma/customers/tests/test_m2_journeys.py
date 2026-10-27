# ruff: noqa: S105, S106 - test credentials, FCM tokens and token-type literals
"""Cross-package customer journeys of the M2 customer API (P1 accounts × P2 commerce × P3 engagement).

Each test walks one real customer flow end to end through the HTTP routes and middleware of
``docs/api/customer-api-v1.md`` ("C§"): the app's device headers, the bearer tokens it got from the auth
routes, the provider callbacks posted exactly as Click / Payme / BTS post them, and the outbox worker running
on commit (``django_capture_on_commit_callbacks(execute=True)``), so pushes, SMS, refunds and shipments happen
as in production. State that has no customer route is arranged through the admin API with a staff token
(cashback adjustments, appointment confirmation, order cancel/transition, campaigns); only the published terms
(fixed versions; the admin publish path has its own journey, ``test_terms_publish_journey.py``) and the
catalogue rows are written directly. Time moves through ``timeutils.now`` only
(no freezegun), and never before a token is issued: PyJWT checks ``iat``/``exp`` against the real clock.

The integration review found five seam defects with these journeys (M2J-1…5: one published-terms rule
for checkout and booking, the hold grace shown only for orders really in it, and ``DELETE me/`` skipping an
order or appointment settled under it); the tests that pinned them now pass.
"""

from __future__ import annotations

import base64
import hashlib
import json
import threading
import uuid
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, nullcontext
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Any
from urllib.parse import parse_qs, quote, urlencode, urlparse

import jwt
import pytest
from django.conf import settings
from django.core.cache import cache
from django.db import connection
from django.test import override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from glowderma.accounts.models import Address, Customer, Device
from glowderma.appointments import services as appointment_services
from glowderma.appointments.models import Appointment, AppointmentEvent
from glowderma.catalog.models import Product
from glowderma.catalog.tests.factories import ProductFactory, ProductStockFactory
from glowderma.commerce import payment_ref
from glowderma.commerce import selectors as commerce_selectors
from glowderma.commerce import services as commerce_services
from glowderma.commerce import tasks as commerce_tasks
from glowderma.commerce.models import CartItem, Order
from glowderma.core import timeutils
from glowderma.core.models import AuditEntry, OutboxMessage
from glowderma.core.timeutils import TASHKENT
from glowderma.integrations.bts import fake as fake_bts
from glowderma.integrations.click import fake as fake_click
from glowderma.integrations.eskiz.registry import fake_eskiz
from glowderma.integrations.fcm import registry as fcm_registry
from glowderma.integrations.fcm.fake import FakeFcm
from glowderma.integrations.fcm.ports import PushMessage
from glowderma.loyalty.models import Purchase
from glowderma.loyalty.tests.support import seed_policies
from glowderma.notifications.models import Campaign
from glowderma.payments.models import Payment, Refund
from glowderma.professionals.models import ProfessionalService
from glowderma.professionals.tests.factories import (
    LocationFactory,
    ProfessionalFactory,
    ProfessionalServiceFactory,
    ServiceFactory,
    WorkingHoursFactory,
)
from glowderma.site_config.models import SiteSettings
from glowderma.site_config.selectors import invalidate_public_settings

API = "/api/v1/"
TERMS_V1 = "2026-10-01"
TERMS_V2 = "2026-11-01"
PASSWORD = "Saffron!Harbor-Long72"
FORM = "application/x-www-form-urlencoded"
NBSP = "\u00a0"

Commit = Callable[[], AbstractContextManager[Any]]


# --- fixtures -----------------------------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def clean_fakes() -> Iterator[None]:
    """The fake Eskiz, BTS and Click keep per-process state: every journey starts from a clean slate."""
    fake_eskiz().sent.clear()
    fake_bts.reset()
    fake_click.reset()
    yield
    fake_eskiz().sent.clear()
    fake_bts.reset()
    fake_click.reset()


@pytest.fixture
def fcm(monkeypatch: pytest.MonkeyPatch) -> FakeFcm:
    """A fresh fake FCM per journey, so ``sent`` holds this journey's pushes only."""
    fake = FakeFcm()
    monkeypatch.setattr(fcm_registry, "_fake", fake)
    return fake


class Clock:
    """``timeutils.now`` under the test's control: real time unless pinned or shifted. A token is only ever
    issued at an instant PyJWT accepts by the wall clock (``iat`` not in the future, ``exp`` not passed)."""

    def __init__(self) -> None:
        self.pinned: datetime | None = None
        self.offset = timedelta(0)

    def now(self) -> datetime:
        return self.pinned if self.pinned is not None else timezone.now() + self.offset

    def at(self, instant: datetime) -> None:
        self.pinned = instant

    def shift(self, offset: timedelta) -> None:
        """A running clock ``offset`` away from the wall clock."""
        self.pinned, self.offset = None, offset

    def release(self) -> None:
        self.pinned, self.offset = None, timedelta(0)


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> Clock:
    pinned = Clock()
    monkeypatch.setattr(timeutils, "now", pinned.now)
    return pinned


@pytest.fixture
def commit(django_capture_on_commit_callbacks: Any) -> Commit:
    """Run each request's on-commit callbacks (tombstones, the eager outbox worker) as a real commit would."""

    def capture() -> AbstractContextManager[Any]:
        return django_capture_on_commit_callbacks(execute=True)  # type: ignore[no-any-return]

    return capture


# --- the app and the staff console --------------------------------------------------------------------------


class App:
    """One install of the mobile app: its device headers, its stored tokens and its device key."""

    def __init__(self, commit: Commit, fid: str, *, platform: str = "ios", lang: str | None = None) -> None:
        self.commit = commit
        self.fid = fid
        self.platform = platform
        self.lang = lang
        self.access: str | None = None
        self.refresh: str | None = None
        self.device_key: str | None = None

    def store(self, tokens: dict[str, Any]) -> None:
        """Persist what the app keeps from ``AuthTokens`` / a refresh (the device key only when issued)."""
        self.access = tokens["access"]
        self.refresh = tokens["refresh"]
        if tokens.get("device_key"):
            self.device_key = tokens["device_key"]

    @property
    def sid(self) -> str:
        assert self.access is not None
        return str(jwt.decode(self.access, options={"verify_signature": False})["sid"])

    def headers(self, *, auth: bool, idem: str | None, extra: dict[str, Any] | None) -> dict[str, Any]:
        headers: dict[str, Any] = {
            "HTTP_X_GD_DEVICE_ID": self.fid,
            "HTTP_X_GD_PLATFORM": self.platform,
            "HTTP_X_GD_APP_VERSION": "1.0.0",
        }
        if self.device_key:
            headers["HTTP_X_GD_DEVICE_KEY"] = self.device_key
        if auth and self.access:
            headers["HTTP_AUTHORIZATION"] = f"Bearer {self.access}"
        if self.lang:
            headers["HTTP_ACCEPT_LANGUAGE"] = self.lang
        if idem:
            headers["HTTP_IDEMPOTENCY_KEY"] = idem
        return headers | (extra or {})

    def call(
        self,
        method: str,
        path: str,
        body: Any = None,
        *,
        auth: bool = True,
        idem: bool | str = False,
        extra: dict[str, Any] | None = None,
    ) -> Any:
        key = str(uuid.uuid4()) if idem is True else (idem or None)
        headers = self.headers(auth=auth, idem=key, extra=extra)
        client = APIClient()
        with self.commit():
            if method == "get":
                return client.get(API + path, body, **headers)
            return getattr(client, method)(API + path, body, format="json", **headers)

    def get(self, path: str, query: dict[str, Any] | None = None, **kw: Any) -> Any:
        return self.call("get", path, query, **kw)

    def post(self, path: str, body: Any = None, **kw: Any) -> Any:
        return self.call("post", path, body, **kw)

    def put(self, path: str, body: Any = None, **kw: Any) -> Any:
        return self.call("put", path, body, **kw)

    def delete(self, path: str, body: Any = None, **kw: Any) -> Any:
        return self.call("delete", path, body, **kw)


class Staff:
    """The admin console: the root ``staff_client`` with the outbox running on commit."""

    def __init__(self, client: APIClient, commit: Commit) -> None:
        self.client = client
        self.commit = commit

    def get(self, path: str, query: dict[str, Any] | None = None) -> Any:
        with self.commit():
            return self.client.get(f"{API}admin/{path}", query)

    def post(self, path: str, body: Any = None, *, multipart: bool = False) -> Any:
        with self.commit():
            return self.client.post(
                f"{API}admin/{path}",
                body if body is not None else {},
                format="multipart" if multipart else "json",
                HTTP_IDEMPOTENCY_KEY=str(uuid.uuid4()),
            )


@dataclass
class World:
    commit: Commit
    fcm: FakeFcm
    staff: Staff
    clock: Clock

    def app(self, fid: str, *, platform: str = "ios", lang: str | None = None) -> App:
        return App(self.commit, fid, platform=platform, lang=lang)


@pytest.fixture
def world(commit: Commit, fcm: FakeFcm, staff_client: APIClient, clock: Clock) -> World:
    seed_policies()  # v3 active: 1 % for 0–20M, earns on the cashback-paid part
    publish_terms(TERMS_V1)
    return World(commit, fcm, Staff(staff_client, commit), clock)


# --- helpers ------------------------------------------------------------------------------------------------


def publish_terms(
    version: str, *, terms_uz: str = "Foydalanish shartlari", privacy_uz: str = "Maxfiylik"
) -> None:
    """Publish the terms at a fixed version (written directly; the admin publish path, which stamps dated
    versions, has its own journey): the singleton row, and the 60 s ``settings/public/``
    cache dropped as ``site_config.services.update_settings`` does on commit."""
    SiteSettings.objects.filter(pk=1).update(
        terms_version=version,
        terms_uz=terms_uz,
        privacy_policy_uz=privacy_uz,
        terms_updated_at=timezone.now(),
    )
    invalidate_public_settings()


def mask(phone: str) -> str:
    return f"+998 {phone[4:6]} *** ** {phone[-2:]}"


def som(amount: int) -> str:
    """The admin ``N()`` rule for uz/ru: NBSP thousands separator (C§4.1)."""
    return f"{amount:,}".replace(",", NBSP)


def iso(instant: datetime) -> str:
    return timeutils.isoformat(instant)


def parse(value: str) -> datetime:
    return datetime.fromisoformat(value)


def verify(app: App, phone: str, purpose: str) -> str:
    """``auth/verify-code/`` with the fixed fake OTP of the test settings (C§2.14.1)."""
    response = app.post(
        "auth/verify-code/",
        {"phone": phone, "purpose": purpose, "code": settings.ESKIZ_FAKE_CODE},
        auth=False,
    )
    assert response.status_code == 200, response.content
    body = response.json()
    assert body["purpose"] == purpose and body["expires_in"] == 600 and len(body["verification_token"]) == 43
    return str(body["verification_token"])


def sign_up(
    app: App, phone: str, *, name: str = "Malika Karimova", language: str | None = None, terms: str = TERMS_V1
) -> dict[str, Any]:
    """send-code (register) → verify-code → set-password with consent → 201 ``AuthTokens``, stored on
    ``app``."""
    sent = app.post("auth/send-code/", {"phone": phone, "purpose": "register"}, auth=False)
    assert sent.status_code == 200, sent.content
    assert sent.json() == {
        "phone_mask": mask(phone),
        "email_mask": None,
        "purpose": "register",
        "expires_in": 120,
        "resend_in": 60,
        "attempts": 5,
        "length": 6,
    }
    assert any(settings.ESKIZ_FAKE_CODE in m.text for m in fake_eskiz().sent if m.phone == phone)
    token = verify(app, phone, "register")
    body: dict[str, Any] = {
        "verification_token": token,
        "password": PASSWORD,
        "name": name,
        "terms_version": terms,
    }
    if language is not None:
        body["language"] = language
    created = app.post("auth/set-password/", body, auth=False)
    assert created.status_code == 201, created.content
    app.store(created.json())
    return dict(created.json())


def register_push(app: App, token: str) -> None:
    """``PUT me/device/`` (the app calls it after every sign-in): the FCM token and push permission."""
    response = app.put(
        "me/device/",
        {"fcm_token": token, "push_enabled": True, "model": "iPhone 15", "os": "iOS 18.1", "locale": "uz"},
    )
    assert response.status_code == 200, response.content
    assert response.json()["push_enabled"] is True and response.json()["fid"] == app.fid


def stocked(price: int, stock: int, *, promo: bool = False) -> Product:
    """A customer-visible product (active, priced, active category) with Billz stock (no catalog API)."""
    product = ProductFactory.create(price=price, billz_price=price, promo=promo)
    ProductStockFactory.create(product=product, quantity=stock, synced_at=timezone.now() - timedelta(hours=1))
    return product


def grant_cashback(world: World, customer_id: int, amount: int) -> None:
    """Admin ``POST customers/{id}/cashback-adjustments/`` (admin §2.6): spendable at once (L4)."""
    response = world.staff.post(
        f"customers/{customer_id}/cashback-adjustments/",
        {"direction": "add", "amount": amount, "reason": "Doimiy mijoz bonusi"},
    )
    assert response.status_code == 201, response.content


def add_to_cart(app: App, product: Product, quantity: int) -> dict[str, Any]:
    response = app.post("cart/items/", {"product_id": product.pk, "quantity": quantity}, idem=True)
    assert response.status_code == 200, response.content
    return dict(response.json())


def add_address(app: App) -> int:
    response = app.post(
        "me/addresses/",
        {
            "label": "Uy",
            "text": "Toshkent, Chilonzor tumani, 9-kvartal, 12-uy",
            "region_code": "10",
            "city_code": "1001",
        },
        idem=True,
    )
    assert response.status_code == 201, response.content
    return int(response.json()["id"])


def checkout(
    app: App,
    *,
    provider: str,
    cashback: int = 0,
    address_id: int | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """``delivery/calculate/`` then ``orders/checkout/`` with the quoted ``expected_total`` (C§2.6,
    §2.7.1)."""
    fulfilment = "delivery" if address_id is not None else "pickup"
    quote = app.post(
        "delivery/calculate/",
        {"fulfilment": fulfilment, "address_id": address_id, "cashback_to_use": cashback},
    )
    assert quote.status_code == 200, quote.content
    assert quote.json()["cashback_to_use"] == cashback
    body = {
        "fulfilment": fulfilment,
        "address_id": address_id,
        "payment_provider": provider,
        "cashback_to_use": cashback,
        "expected_total": quote.json()["expected_total"],
        "customer_note": "",
    }
    placed = app.post("orders/checkout/", body, idem=True, extra=extra)
    assert placed.status_code == 201, placed.content
    order = dict(placed.json())
    assert order["status"] == "awaiting_payment" and order["actions"]["pay"] is True
    assert order["total"] == quote.json()["expected_total"]
    return order


def pay(app: App, order_id: int, provider: str | None = None) -> dict[str, Any]:
    response = app.post(f"orders/{order_id}/pay/", {"provider": provider}, idem=True)
    assert response.status_code == 200, response.content
    return dict(response.json())


def sign_time() -> str:
    return timeutils.now().astimezone(TASHKENT).strftime("%Y-%m-%d %H:%M:%S")


def click_params(
    *, txn: int, ref: str, amount: str, action: str, prepare_id: int | None = None, error: int = 0
) -> dict[str, str]:
    """What Click's SHOP API posts, signed per C§2.13.1: MD5 over the raw strings, no separators."""
    params = {
        "click_trans_id": str(txn),
        "service_id": str(settings.CLICK_SERVICE_ID),
        "click_paydoc_id": str(txn + 7_000_000),
        "merchant_trans_id": ref,
        "amount": amount,
        "action": action,
        "error": str(error),
        "error_note": "Success" if error == 0 else "Insufficient funds",
        "sign_time": sign_time(),
    }
    parts = [params["click_trans_id"], params["service_id"], str(settings.CLICK_SECRET_KEY), ref]
    if prepare_id is not None:
        params["merchant_prepare_id"] = str(prepare_id)
        parts.append(params["merchant_prepare_id"])
    parts += [amount, action, params["sign_time"]]
    params["sign_string"] = hashlib.md5("".join(parts).encode(), usedforsecurity=False).hexdigest()
    return params


def click(world: World, step: str, params: dict[str, str]) -> dict[str, Any]:
    with world.commit():
        response = APIClient().generic(
            "POST", f"{API}webhooks/click/{step}/", urlencode(params), content_type=FORM
        )
    assert response.status_code == 200, response.content
    return dict(response.json())


def click_prepare(world: World, *, txn: int, ref: str, amount: int) -> dict[str, Any]:
    return click(world, "prepare", click_params(txn=txn, ref=ref, amount=str(amount), action="0"))


def click_complete(world: World, *, txn: int, ref: str, amount: int, prepare_id: int) -> dict[str, Any]:
    params = click_params(txn=txn, ref=ref, amount=f"{amount}.00", action="1", prepare_id=prepare_id)
    return click(world, "complete", params)


_rpc_ids = iter(range(1, 10_000))


def payme(world: World, method: str, params: dict[str, Any]) -> dict[str, Any]:
    """A Payme Merchant API call: JSON-RPC 2.0 with ``Basic base64("Paycom:" + PAYME_KEY)`` (C§2.13.2)."""
    rpc_id = next(_rpc_ids)
    body = {"jsonrpc": "2.0", "id": rpc_id, "method": method, "params": params}
    secret = base64.b64encode(f"Paycom:{settings.PAYME_KEY}".encode()).decode()
    with world.commit():
        response = APIClient().generic(
            "POST",
            f"{API}webhooks/payme/",
            json.dumps(body),
            content_type="application/json",
            HTTP_AUTHORIZATION=f"Basic {secret}",
        )
    assert response.status_code == 200, response.content
    answer = dict(response.json())
    assert answer["jsonrpc"] == "2.0" and answer["id"] == rpc_id
    return answer


def epoch_ms(instant: datetime | None = None) -> int:
    return int((instant or timeutils.now()).timestamp() * 1000)


def pushes(world: World, kind: str) -> list[PushMessage]:
    return [message for message in world.fcm.sent if message.data.get("type") == kind]


def inbox(app: App) -> dict[str, Any]:
    response = app.get("me/notifications/")
    assert response.status_code == 200, response.content
    return dict(response.json())


def order_detail(app: App, order_id: int) -> dict[str, Any]:
    response = app.get(f"me/orders/{order_id}/")
    assert response.status_code == 200, response.content
    return dict(response.json())


def cashback(app: App) -> dict[str, Any]:
    response = app.get("me/cashback/")
    assert response.status_code == 200, response.content
    return dict(response.json())


def ledger(app: App) -> list[dict[str, Any]]:
    response = app.get("me/cashback/ledger/")
    assert response.status_code == 200, response.content
    return list(response.json()["results"])


def available_stock(app: App, product: Product) -> int:
    response = app.get(f"catalog/products/{product.pk}/")
    assert response.status_code == 200, response.content
    return int(response.json()["available"])


def error(response: Any, status: int, code: str) -> dict[str, Any]:
    assert response.status_code == status, response.content
    body = dict(response.json())
    assert body["code"] == code, body
    return body


def outbox_clean() -> None:
    """Every outbox message the journey produced was handled: none failed or dead (a handler that cannot read
    another package's payload shows up here)."""
    broken = list(
        OutboxMessage.objects.exclude(status__in=["pending", "done"]).values("topic", "status", "last_error")
    )
    assert broken == [], broken


def specialist(day: date) -> ProfessionalService:
    """A dermatologist working 09:00–18:00 (break 13–14) on ``day``'s weekday, one 30-minute service."""
    location = LocationFactory.create(name_uz="Glow Derma Yunusobod")
    professional = ProfessionalFactory.create(location=location, name="Nodira Rahimova", slot_step_min=30)
    service = ServiceFactory.create(name_uz="Dermatolog konsultatsiyasi", default_duration_min=30)
    WorkingHoursFactory.create(
        professional=professional,
        weekday=day.weekday(),
        start=time(9, 0),
        end=time(18, 0),
        break_start=time(13, 0),
        break_end=time(14, 0),
    )
    return ProfessionalServiceFactory.create(
        professional=professional, service=service, price=150_000, duration_min=30
    )


def booking_day() -> date:
    return timeutils.local_date(timezone.now()) + timedelta(days=3)


def slot(day: date, hour: int, minute: int = 0) -> datetime:
    return datetime.combine(day, time(hour, minute), tzinfo=TASHKENT)


def book(app: App, link: ProfessionalService, start: datetime) -> Any:
    body = {"specialist_id": link.professional_id, "service_id": link.service_id, "start": start.isoformat()}
    return app.post("appointments/", body, idem=True)


def without_request_id(body: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in body.items() if key != "request_id"}


# --- journeys -----------------------------------------------------------------------------------------------


@pytest.mark.django_db
def test_journey_1_register_second_device_refresh_reuse_and_logout(world: World) -> None:
    """C§2.1.1–2.1.7, §1.9: sign-up on one install, password sign-in on a second install with the new-device
    SMS code (A2), the first install signed out with ``SESSION_REPLACED`` (+ the data-only push carrying its
    sid), refresh rotation with the grace replay, logout, the known-device sign-in without SMS, and refresh
    reuse revoking the session."""
    phone = "+998901230011"
    first = world.app("fid-journey1-first")
    public = first.get("settings/public/", auth=False)
    assert public.status_code == 200 and public.json()["terms_version"] == TERMS_V1

    created = sign_up(first, phone)
    assert created["is_new_customer"] is True and created["reactivated"] is False
    assert len(created["device_key"]) == 43
    assert created["customer"]["phone"] == phone and created["customer"]["has_password"] is True
    assert created["customer"]["consent"]["update_required"] is False
    assert first.get("me/").json()["id"] == created["customer"]["id"]
    register_push(first, "fcm-journey1-first")

    # a second install: the password alone is not enough on an unknown device (A2)
    second = world.app("fid-journey1-second", platform="android")
    step = second.post("auth/login/", {"phone": phone, "password": PASSWORD}, auth=False)
    assert step.status_code == 200, step.content
    assert step.json() == {
        "next_step": "verify_device",
        "phone_mask": mask(phone),
        "otp": {"expires_in": 120, "resend_in": 60, "length": 6},
        "social_ticket": None,
        "provider": None,
        "email_mask": None,
        "expires_in": step.json()["expires_in"],
    }
    assert len([m for m in fake_eskiz().sent if m.phone == phone]) == 2  # the register and the login code
    token = verify(second, phone, "login_new_device")
    signed = second.post(
        "auth/login/", {"phone": phone, "password": PASSWORD, "verification_token": token}, auth=False
    )
    assert signed.status_code == 200, signed.content
    assert signed.json()["is_new_customer"] is False and len(signed.json()["device_key"]) == 43
    assert signed.json()["device_key"] != first.device_key
    second.store(signed.json())

    # B-W1: the first install stays signed in next to the second one; nobody is told to sign out
    assert first.get("me/").status_code == 200
    assert pushes(world, "session_replaced") == []
    assert Device.objects.get(fid=first.fid).fcm_token == "fcm-journey1-first"
    listed = second.get("me/sessions/").json()["results"]
    assert {row["platform"] for row in listed} == {"ios", "android"}
    assert [row["current"] for row in listed if row["platform"] == "android"] == [True]

    # refresh rotates; the old token replays the very same tokens inside the grace window
    old = second.refresh
    rotated = second.post("auth/refresh/", {"refresh": old}, auth=False)
    assert rotated.status_code == 200, rotated.content
    assert set(rotated.json()) == {"access", "access_expires_at", "refresh", "refresh_expires_at"}
    assert rotated.json()["refresh"] != old
    second.store(rotated.json())
    assert second.get("me/").status_code == 200  # first use of the new rotation confirms it
    replay = second.post("auth/refresh/", {"refresh": old}, auth=False)
    assert replay.status_code == 200 and replay.json() == rotated.json()

    # logout revokes the session; the app is signed out everywhere
    assert second.post("auth/logout/", {"refresh": second.refresh}, auth=False).status_code == 204
    error(second.get("me/"), 401, "SESSION_EXPIRED")
    error(second.post("auth/refresh/", {"refresh": second.refresh}, auth=False), 401, "SESSION_EXPIRED")
    error(second.post("auth/logout/", {"refresh": second.refresh}, auth=False), 401, "SESSION_EXPIRED")

    # signing in again on this install: the kept device key makes it known — no SMS, no new key
    texts_before = len(fake_eskiz().sent)
    again = second.post("auth/login/", {"phone": phone, "password": PASSWORD}, auth=False)
    assert again.status_code == 200, again.content
    assert again.json()["device_key"] is None and len(fake_eskiz().sent) == texts_before
    second.store(again.json())

    # refresh reuse: an old token presented after the rotation was confirmed and the grace is over kills it
    stolen = second.refresh
    assert stolen is not None
    rotated = second.post("auth/refresh/", {"refresh": stolen}, auth=False)
    assert rotated.status_code == 200, rotated.content
    second.store(rotated.json())
    assert second.get("me/").status_code == 200
    cache.delete(f"auth:refresh_grace:{hashlib.sha256(stolen.encode()).hexdigest()}")  # the 120 s are over
    error(second.post("auth/refresh/", {"refresh": stolen}, auth=False), 401, "SESSION_EXPIRED")
    error(second.post("auth/refresh/", {"refresh": second.refresh}, auth=False), 401, "SESSION_EXPIRED")
    error(second.get("me/"), 401, "SESSION_EXPIRED")
    assert AuditEntry.objects.filter(action="customer.refresh_reused").count() == 1
    outbox_clean()


@pytest.mark.django_db
def test_journey_2_cart_checkout_click_and_order_timeline(world: World) -> None:
    """C§2.5–2.8, §2.9, §2.12, §2.13.1, §4: cart → quote → checkout with cashback → pay link → Click Prepare
    and Complete as Click posts them → order ``processing``, hold captured, credit posted, BTS shipment
    created, ``push.order_status`` inbox row with the customer label, timeline, progress and ledger."""
    phone = "+998901230022"
    app = world.app("fid-journey2-phone")
    customer_id = int(sign_up(app, phone)["customer"]["id"])
    register_push(app, "fcm-journey2")
    grant_cashback(world, customer_id, 50_000)
    assert cashback(app)["available"] == 50_000

    serum = stocked(300_000, 5)
    promo = stocked(150_000, 3, promo=True)
    first = app.post(
        "cart/items/", {"product_id": serum.pk, "quantity": 2}, idem="d1b0c0de-0000-4000-8000-000000000001"
    )
    assert first.status_code == 200, first.content
    retry = app.post(
        "cart/items/", {"product_id": serum.pk, "quantity": 2}, idem="d1b0c0de-0000-4000-8000-000000000001"
    )
    assert retry.json()["items"][0]["quantity"] == 2  # a retried "add 2" replays, it never adds again
    cart = add_to_cart(app, promo, 1)
    assert (cart["item_count"], cart["items_subtotal"], cart["has_problems"]) == (2, 750_000, False)
    assert cart["cashback"]["available"] == 50_000
    address_id = add_address(app)
    regions = app.get("delivery/regions/").json()["regions"]
    assert any(r["code"] == "10" and any(c["code"] == "1001" for c in r["cities"]) for r in regions)

    order = checkout(app, provider="click", cashback=50_000, address_id=address_id)
    order_id = int(order["id"])
    total = int(order["total"])
    assert order["delivery_fee"] > 0 and total == 750_000 + order["delivery_fee"] - 50_000
    assert order["cashback"]["used"] == 50_000 and order["payment"]["status"] == "pending"
    assert [entry["type"] for entry in order["timeline"]] == ["order_created"]
    emptied = app.get("cart/").json()
    held = cashback(app)
    assert (held["balance"], held["held"], held["available"]) == (50_000, 50_000, 0)
    # the cart (P2) shows the same spendable cashback as the cashback card (P3)
    assert emptied["items"] == [] and emptied["cashback"]["available"] == held["available"]
    assert held["holds"] == [{"order_id": order_id, "amount": 50_000, "expires_at": order["pay_by"]}]
    assert available_stock(app, serum) == 3

    link = pay(app, order_id)
    assert (link["order_id"], link["provider"], link["amount"], link["app_url"]) == (
        order_id,
        "click",
        total,
        None,
    )
    assert link["return_url"] == f"{settings.PAYMENT_RETURN_URL}?order_id={order_id}"
    query = parse_qs(urlparse(link["web_url"]).query)
    assert query["transaction_param"] == [link["account_ref"]] and query["amount"] == [str(total)]
    assert query["return_url"] == [link["return_url"]] and query["service_id"] == [
        str(settings.CLICK_SERVICE_ID)
    ]
    assert link["account_ref"].startswith(f"{order_id}-") and len(link["account_ref"].split("-")[1]) == 10

    prepared = click_prepare(world, txn=2_875_510_001, ref=link["account_ref"], amount=total)
    assert prepared == {
        "click_trans_id": 2_875_510_001,
        "merchant_trans_id": link["account_ref"],
        "merchant_prepare_id": link["payment_id"],
        "error": 0,
        "error_note": "Success",
    }
    pending = order_detail(app, order_id)
    assert (
        pending["payment"]["in_progress"] is True
        and pending["payment"]["in_progress_label"] == "To‘lov tekshirilmoqda"
    )
    assert pending["actions"] == {"pay": True, "cancel": False, "reorder": False}

    completed = click_complete(
        world, txn=2_875_510_001, ref=link["account_ref"], amount=total, prepare_id=link["payment_id"]
    )
    assert completed == {
        "click_trans_id": 2_875_510_001,
        "merchant_trans_id": link["account_ref"],
        "merchant_confirm_id": link["payment_id"],
        "error": 0,
        "error_note": "Success",
    }
    retried = click_complete(
        world, txn=2_875_510_001, ref=link["account_ref"], amount=total, prepare_id=link["payment_id"]
    )
    assert retried["error"] == 0 and retried["merchant_confirm_id"] == link["payment_id"]  # re-signed retry
    assert Purchase.objects.filter(order_id=order_id).count() == 1

    # the ledger: admin bonus, the debit of the used cashback and the credit of the purchase
    entries = ledger(app)
    assert [(e["kind"], e["source"], e["order_id"]) for e in entries] == [
        ("credit", "online", order_id),
        ("debit", "online", order_id),
        ("adjustment", "admin", None),
    ]
    earned = int(entries[0]["amount"])
    assert earned > 0 and entries[1]["amount"] == -50_000
    assert (entries[0]["title"], entries[0]["subtitle"]) == (
        "Cashback hisoblandi",
        f"Buyurtma №{order_id} · 1%",
    )
    assert (entries[1]["title"], entries[1]["subtitle"]) == ("Cashback ishlatildi", f"Buyurtma №{order_id}")
    assert (entries[2]["title"], entries[2]["subtitle"]) == ("Bonus qo‘shildi", None)
    after = cashback(app)
    assert (after["balance"], after["held"], after["available"], after["holds"]) == (earned, 0, earned, [])

    # the customer's order: paid, shipment created by the BTS outbox handler, timeline and progress
    detail = order_detail(app, order_id)
    assert (detail["status"], detail["status_label"]) == ("processing", "Tayyorlanmoqda")
    assert detail["payment"]["status"] == "paid" and detail["payment"]["status_label"] == "To‘langan"
    assert detail["payment"]["in_progress"] is False and detail["payment"]["confirmed_at"] is not None
    assert detail["cashback"] == {"used": 50_000, "earned": earned, "earned_state": "credited"}
    tracking = detail["shipment"]["tracking_number"]
    assert (
        detail["shipment"] == {"carrier": "BTS", "tracking_number": tracking, "tracking_url": None}
        and tracking
    )
    assert [(e["type"], e["title"], e["subtitle"]) for e in detail["timeline"]] == [
        ("shipment_created", "Yetkazib berishga topshirildi", f"BTS kuzatuv raqami: {tracking}"),
        ("payment_confirmed", "To‘lov qabul qilindi", f"Click orqali · cashback +{som(earned)} so‘m"),
        ("order_created", "Buyurtma berildi", f"{detail['item_count']} ta mahsulot · {som(total)} so‘m"),
    ]
    assert [(p["step"], p["state"]) for p in detail["progress"]] == [
        ("placed", "done"),
        ("paid", "done"),
        ("with_courier", "current"),
        ("delivered", "upcoming"),
    ]
    assert detail["actions"] == {"pay": False, "cancel": False, "reorder": True}
    summary = app.get("me/orders/").json()["results"]
    assert [(o["id"], o["status"], o["payment"]["status"], o["tracking_number"]) for o in summary] == [
        (order_id, "processing", "paid", tracking)
    ]
    assert available_stock(app, serum) == 3  # the reservation was converted, not released

    # push.order_status: one inbox row with the customer label, and the push carrying its inbox_id
    feed = inbox(app)
    assert feed["unread_count"] == 1 and len(feed["results"]) == 1
    row = feed["results"][0]
    assert (row["kind"], row["title"]) == ("order_status", "Tayyorlanmoqda")
    assert row["body"] == f"To‘lov qabul qilindi. Buyurtma №{order_id} tayyorlanmoqda."
    assert row["link"] == {
        "kind": "order",
        "product_id": None,
        "order_id": order_id,
        "appointment_id": None,
        "url": None,
    }
    [message] = pushes(world, "order_status")
    assert message.token == "fcm-journey2" and message.title == "Tayyorlanmoqda"
    assert message.data == {
        "type": "order_status",
        "inbox_id": str(row["id"]),
        "order_id": str(order_id),
        "status": "processing",
    }
    assert app.post("me/notifications/ack/", {"inbox_ids": [row["id"]]}).json() == {"acknowledged": 1}
    read = app.post(f"me/notifications/{row['id']}/read/")
    assert read.status_code == 200 and read.json()["unread_count"] == 0
    topics = set(OutboxMessage.objects.filter(payload__order_id=order_id).values_list("topic", "status"))
    assert {
        ("billz.push_sale", "done"),
        ("bts.create_shipment", "done"),
        ("push.order_status", "done"),
    } <= topics
    assert world.staff.get(f"orders/{order_id}/").json()["status"] == "processing"
    outbox_clean()


@pytest.mark.django_db
def test_journey_3_payme_state_machine_with_refund_after_perform(world: World) -> None:
    """C§2.13.2 and §2.7.2 for a Russian-speaking customer: pay link → CheckPerform → Create (idempotent,
    -31051 for a second id, Click refused meanwhile) → Perform (idempotent) → CheckTransaction → Cancel after
    perform refused (-31007) until staff cancel the order (Payme refund ``manual_required``) → Cancel → refund
    succeeded, payment ``refunded``, timeline "Деньги возвращены" → GetStatement."""
    phone = "+998901230033"
    app = world.app("fid-journey3-phone")
    sign_up(app, phone, language="ru")
    register_push(app, "fcm-journey3")
    cream = stocked(240_000, 4)
    add_to_cart(app, cream, 1)
    order = checkout(app, provider="payme")
    order_id, total = int(order["id"]), int(order["total"])
    tiyin = total * 100

    link = pay(app, order_id)
    ref = link["account_ref"]
    assert link["provider"] == "payme" and link["amount"] == total
    encoded = link["web_url"].removeprefix(f"{settings.PAYME_CHECKOUT_URL}/")
    fields = dict(part.split("=", 1) for part in base64.b64decode(encoded).decode().split(";"))
    assert fields == {
        "m": str(settings.PAYME_MERCHANT_ID),
        f"ac.{settings.PAYME_ACCOUNT_FIELD}": ref,
        "a": str(tiyin),
        "l": "ru",
        "c": quote(link["return_url"], safe=""),
        "ct": "15000",
    }
    account = {str(settings.PAYME_ACCOUNT_FIELD): ref}

    assert payme(world, "CheckPerformTransaction", {"amount": tiyin, "account": account})["result"] == {
        "allow": True
    }
    wrong = payme(world, "CheckPerformTransaction", {"amount": tiyin + 100, "account": account})
    assert wrong["error"]["code"] == -31001
    unknown = payme(
        world, "CheckPerformTransaction", {"amount": tiyin, "account": {"order_id": f"{order_id}-0000000000"}}
    )
    assert (unknown["error"]["code"], unknown["error"]["data"]) == (-31050, "order_id")

    payme_time = epoch_ms()
    created = payme(
        world,
        "CreateTransaction",
        {"id": "pm-journey3-1", "time": payme_time, "amount": tiyin, "account": account},
    )
    assert created["result"]["state"] == 1 and created["result"]["transaction"] == str(link["payment_id"])
    again = payme(
        world,
        "CreateTransaction",
        {"id": "pm-journey3-1", "time": payme_time, "amount": tiyin, "account": account},
    )
    assert again["result"] == created["result"]
    busy = payme(
        world,
        "CreateTransaction",
        {"id": "pm-journey3-2", "time": epoch_ms(), "amount": tiyin, "account": account},
    )
    assert (busy["error"]["code"], busy["error"]["data"]) == (-31051, "order_id")
    assert (
        payme(world, "CheckPerformTransaction", {"amount": tiyin, "account": account})["error"]["code"]
        == -31051
    )

    # while Payme holds a state-1 transaction the app cannot switch to Click and Click cannot attach
    waiting = order_detail(app, order_id)
    assert waiting["payment"]["in_progress"] is True and waiting["actions"]["pay"] is False
    switched = error(
        app.post(f"orders/{order_id}/pay/", {"provider": "click"}, idem=True), 409, "payment_in_progress"
    )
    assert switched["details"] == {"provider": "payme", "order_id": order_id}
    assert switched["message"] == "Оплата в Payme уже идёт. Завершите её или подождите."
    assert click_prepare(world, txn=2_875_530_001, ref=ref, amount=total)["error"] == -9

    performed = payme(world, "PerformTransaction", {"id": "pm-journey3-1"})["result"]
    assert (
        performed["state"] == 2
        and performed["transaction"] == str(link["payment_id"])
        and performed["perform_time"] > 0
    )
    assert payme(world, "PerformTransaction", {"id": "pm-journey3-1"})["result"] == performed
    assert Purchase.objects.filter(order_id=order_id).count() == 1
    checked = payme(world, "CheckTransaction", {"id": "pm-journey3-1"})["result"]
    assert checked == {
        "create_time": created["result"]["create_time"],
        "perform_time": performed["perform_time"],
        "cancel_time": 0,
        "transaction": str(link["payment_id"]),
        "state": 2,
        "reason": None,
    }
    paid = order_detail(app, order_id)
    assert (paid["status"], paid["status_label"]) == ("processing", "Готовится")
    [processing] = pushes(world, "order_status")
    assert processing.title == "Готовится" and processing.data["status"] == "processing"
    earning = cashback(app)
    assert earning["lifetime_total"] == total and earning["balance"] == paid["cashback"]["earned"] > 0
    assert payme(world, "CancelTransaction", {"id": "pm-journey3-1", "reason": 5})["error"]["code"] == -31007

    # staff cancel the paid order: a Payme refund waits for the cabinet (manual_required)
    cancelled = world.staff.post(f"orders/{order_id}/cancel/", {"reason": "Mijoz so‘rovi bilan"})
    assert cancelled.status_code == 200, cancelled.content
    refunding = order_detail(app, order_id)
    assert (refunding["status"], refunding["status_label"]) == ("cancelled", "Отменён")
    assert refunding["cancellation"] == {"reason": "admin", "label": "Отменён магазином"}
    assert (
        refunding["refund"]["state"] == "in_progress"
        and refunding["refund"]["state_label"] == "Возврат в процессе"
    )
    assert refunding["refund"]["amount"] == total and refunding["cashback"]["earned_state"] == "reversed"
    reverted = cashback(app)  # the admin cancel reversed the purchase (P2) and its credit (loyalty)
    assert (reverted["lifetime_total"], reverted["balance"]) == (0, 0)
    assert (
        refunding["timeline"][0]["type"],
        refunding["timeline"][0]["title"],
        refunding["timeline"][0]["subtitle"],
    ) == (
        "cancelled",
        "Заказ отменён",
        "Отменён магазином · деньги будут возвращены",
    )

    # the cabinet refund: Payme cancels the performed transaction
    reversed_ = payme(world, "CancelTransaction", {"id": "pm-journey3-1", "reason": 5})["result"]
    assert (
        reversed_["state"] == -2
        and reversed_["transaction"] == str(link["payment_id"])
        and reversed_["cancel_time"] > 0
    )
    assert payme(world, "CancelTransaction", {"id": "pm-journey3-1", "reason": 5})["result"] == reversed_
    final = payme(world, "CheckTransaction", {"id": "pm-journey3-1"})["result"]
    assert (final["state"], final["reason"], final["cancel_time"]) == (-2, 5, reversed_["cancel_time"])
    refunded = order_detail(app, order_id)
    assert (refunded["payment"]["status"], refunded["payment"]["status_label"]) == ("refunded", "Возвращён")
    assert (
        refunded["refund"]["state"] == "completed"
        and refunded["refund"]["state_label"] == "Деньги возвращены"
    )
    assert (
        refunded["timeline"][0]["type"],
        refunded["timeline"][0]["title"],
        refunded["timeline"][0]["subtitle"],
    ) == (
        "refund_succeeded",
        "Деньги возвращены",
        f"{som(total)} сум · Payme",
    )
    assert Payment.objects.get(pk=link["payment_id"]).status == "refunded"

    statement = payme(world, "GetStatement", {"from": payme_time - 1, "to": payme_time + 1})["result"][
        "transactions"
    ]
    assert statement == [
        {
            "id": "pm-journey3-1",
            "time": payme_time,
            "amount": tiyin,
            "account": account,
            "create_time": created["result"]["create_time"],
            "perform_time": performed["perform_time"],
            "cancel_time": reversed_["cancel_time"],
            "transaction": str(link["payment_id"]),
            "state": -2,
            "reason": 5,
            "receivers": None,
        }
    ]
    titles = [row["title"] for row in inbox(app)["results"]]
    assert titles == ["Отменён", "Готовится"]
    outbox_clean()


@pytest.mark.django_db
def test_journey_4_expiry_then_late_click_complete_is_accepted_and_reversed(world: World) -> None:
    """C§2.7.2 (payable, grace), §2.9.1 (effective hold expiry), §2.13.0 (refused capture), §2.8 (display
    rule), §4.1: Prepare before ``R``; the expiry job skips the order during the grace and the hold shows the
    graced expiry; after the grace the job cancels it (hold and stock released); the late Complete is accepted
    (``0``) and reversed through the Click refund; the customer sees the late capture as refunded."""
    phone = "+998901230044"
    app = world.app("fid-journey4-phone")
    customer_id = int(sign_up(app, phone)["customer"]["id"])
    register_push(app, "fcm-journey4")
    grant_cashback(world, customer_id, 10_000)
    toner = stocked(120_000, 2)
    add_to_cart(app, toner, 1)
    order = checkout(app, provider="click", cashback=10_000)
    order_id, total = int(order["id"]), int(order["total"])
    deadline = parse(order["pay_by"])
    link = pay(app, order_id)
    ref = link["account_ref"]
    assert click_prepare(world, txn=2_875_540_001, ref=ref, amount=total)["error"] == 0

    # one second after R: the open Click transaction attached in time keeps the order for the grace
    world.clock.at(deadline + timedelta(seconds=1))
    with world.commit():
        assert commerce_tasks.expire_reservations() == []
    grace = cashback(app)
    assert grace["holds"] == [
        {"order_id": order_id, "amount": 10_000, "expires_at": iso(deadline + timedelta(seconds=300))}
    ]
    expired = error(app.post(f"orders/{order_id}/pay/", {"provider": None}, idem=True), 409, "order_expired")
    assert (
        expired["message"]
        == "To‘lov muddati tugadi. Mahsulotlarni savatga qaytarib, qaytadan rasmiylashtiring."
    )
    waiting = order_detail(app, order_id)
    assert waiting["status"] == "awaiting_payment" and waiting["actions"]["pay"] is False

    # after the grace the job expires it: hold and stock released (an expiry sends no push, N-1)
    world.clock.at(deadline + timedelta(seconds=301))
    with world.commit():
        assert commerce_tasks.expire_reservations() == [order_id]
    released = cashback(app)
    assert (released["balance"], released["held"], released["available"]) == (10_000, 0, 10_000)
    assert available_stock(app, toner) == 2
    gone = order_detail(app, order_id)
    assert gone["status"] == "cancelled" and gone["cancellation"] == {
        "reason": "reservation_expired",
        "label": "To‘lov muddati tugadi",
    }
    assert (gone["timeline"][0]["type"], gone["timeline"][0]["subtitle"]) == (
        "cancelled",
        "To‘lov muddati tugadi",
    )
    assert gone["actions"] == {"pay": False, "cancel": False, "reorder": True}

    # the customer had confirmed at Click after all: Click's Complete must be accepted and reversed
    world.clock.at(deadline + timedelta(seconds=400))
    late = click_complete(world, txn=2_875_540_001, ref=ref, amount=total, prepare_id=link["payment_id"])
    assert (late["error"], late["merchant_confirm_id"]) == (0, link["payment_id"])
    audit = AuditEntry.objects.get(action="payment.late_capture")
    assert audit.after == {
        "order_id": order_id,
        "payment_id": link["payment_id"],
        "provider": "click",
        "case": "failed_by_us",
    }
    refund = Refund.objects.get(payment_id=link["payment_id"])
    assert (refund.origin, refund.state, refund.amount) == ("late_capture", "succeeded", total)
    assert ("refund", str(2_875_540_001 + 7_000_000)) in fake_click.calls  # reversed by click_paydoc_id
    shown = order_detail(app, order_id)
    assert shown["status"] == "cancelled"  # a refused capture never changes the order
    assert shown["payment"]["status"] == "refunded" and shown["payment"]["provider"] == "click"
    assert shown["refund"]["state"] == "completed" and shown["refund"]["amount"] == total
    assert [e["type"] for e in shown["timeline"]][:3] == [
        "refund_succeeded",
        "refund_in_progress",
        "cancelled",
    ]
    assert shown["timeline"][0]["subtitle"] == f"{som(total)} so‘m · Click"
    assert cashback(app)["balance"] == 10_000 and available_stock(app, toner) == 2
    assert Purchase.objects.filter(order_id=order_id).count() == 0

    # a new attempt cannot attach any more, and the lines go back into the cart on request
    world.clock.at(deadline + timedelta(seconds=500))
    assert click_prepare(world, txn=2_875_540_002, ref=ref, amount=total)["error"] == -9
    world.clock.release()
    refill = app.post(f"orders/{order_id}/reorder/", idem=True)
    assert refill.status_code == 200, refill.content
    assert [(i["product_id"], i["quantity"]) for i in refill.json()["items"]] == [(toner.pk, 1)]
    assert inbox(app)["results"] == [] and pushes(world, "order_status") == []  # expiry never pushes (N-1)
    outbox_clean()


@pytest.mark.django_db
def test_journey_4b_hold_without_an_attached_transaction_shows_its_own_expiry(world: World) -> None:
    """C§2.9.1: a hold's EFFECTIVE expiry is ``reservation_expires_at``, plus the grace only "while a
    transaction attached in time is still open" (the rule P2's expiry job applies, §2.7.2). Between ``R`` and
    the next run of the expiry job (beat every 60 s) an order nobody started paying must not show its cashback
    as held for five more minutes."""
    phone = "+998901230045"
    app = world.app("fid-journey4b-phone")
    customer_id = int(sign_up(app, phone)["customer"]["id"])
    grant_cashback(world, customer_id, 10_000)
    add_to_cart(app, stocked(120_000, 2), 1)
    order = checkout(app, provider="click", cashback=10_000)
    deadline = parse(order["pay_by"])
    world.clock.at(deadline + timedelta(seconds=1))  # the beat has not run the expiry job yet
    holds = cashback(app)["holds"]
    assert holds == [{"order_id": int(order["id"]), "amount": 10_000, "expires_at": order["pay_by"]}]


@pytest.mark.django_db
def test_journey_4c_payme_expiry_perform_refused_then_payme_cancels(world: World) -> None:
    """C§2.7.2 (P-2), §2.13.2, §4.1: a Payme transaction created before ``R`` keeps the order for the
    grace; after it the order expires while the Payme payment stays pending; Payme's Perform is refused
    (-31008, the transaction stays in state 1, no money taken), Payme's timeout cancel fails the payment, the
    timeline shows "To‘lov amalga oshmadi · Payme", hold and stock are back and reorder refills the cart."""
    phone = "+998901230046"
    app = world.app("fid-journey4c-phone")
    customer_id = int(sign_up(app, phone)["customer"]["id"])
    grant_cashback(world, customer_id, 7_000)
    cleanser = stocked(110_000, 1)
    add_to_cart(app, cleanser, 1)
    order = checkout(app, provider="payme", cashback=7_000)
    order_id, total = int(order["id"]), int(order["total"])
    deadline = parse(order["pay_by"])
    link = pay(app, order_id)
    account = {str(settings.PAYME_ACCOUNT_FIELD): link["account_ref"]}
    params = {"id": "pm-journey4c-1", "time": epoch_ms(), "amount": total * 100, "account": account}
    created = payme(world, "CreateTransaction", params)["result"]
    assert created["state"] == 1

    world.clock.at(deadline + timedelta(seconds=1))
    with world.commit():
        assert commerce_tasks.expire_reservations() == []  # a state-1 transaction attached in time
    world.clock.at(deadline + timedelta(seconds=301))
    with world.commit():
        assert commerce_tasks.expire_reservations() == [order_id]
    assert Payment.objects.get(pk=link["payment_id"]).status == "pending"  # Payme debits only on Perform
    released = cashback(app)
    assert (released["held"], released["available"]) == (0, 7_000) and available_stock(app, cleanser) == 1

    refused = payme(world, "PerformTransaction", {"id": "pm-journey4c-1"})
    assert refused["error"]["code"] == -31008
    assert payme(world, "CheckTransaction", {"id": "pm-journey4c-1"})["result"]["state"] == 1
    world.clock.at(deadline + timedelta(seconds=400))
    cancelled = payme(world, "CancelTransaction", {"id": "pm-journey4c-1", "reason": 4})["result"]
    assert cancelled["state"] == -1 and cancelled["cancel_time"] > 0
    checked = payme(world, "CheckTransaction", {"id": "pm-journey4c-1"})["result"]
    assert (checked["state"], checked["reason"], checked["perform_time"]) == (-1, 4, 0)
    detail = order_detail(app, order_id)
    assert detail["status"] == "cancelled" and detail["payment"]["status"] == "failed"
    assert detail["cancellation"] == {"reason": "reservation_expired", "label": "To‘lov muddati tugadi"}
    assert (
        detail["timeline"][0]["type"],
        detail["timeline"][0]["title"],
        detail["timeline"][0]["subtitle"],
    ) == (
        "payment_failed",
        "To‘lov amalga oshmadi",
        "Payme",
    )
    assert Purchase.objects.filter(order_id=order_id).count() == 0
    world.clock.release()
    refill = app.post(f"orders/{order_id}/reorder/", idem=True)
    assert refill.status_code == 200 and [i["product_id"] for i in refill.json()["items"]] == [cleanser.pk]
    outbox_clean()


@pytest.mark.django_db
def test_journey_5_qr_is_single_use_and_dies_with_the_session(world: World, pos_client: APIClient) -> None:
    """C§2.9.3, §2.10, §1.9: the personal QR resolves once at the cashier (no name, no id, the spendable
    cashback and tier), a second scan is ``qr_invalid``; a token outlives neither a logout nor its session
    being signed out from another device (B-W1: a sign-in elsewhere keeps it); a resolve records nothing
    but the audit row."""
    phone = "+998901230055"
    app = world.app("fid-journey5-phone")
    customer_id = int(sign_up(app, phone)["customer"]["id"])
    grant_cashback(world, customer_id, 48_600)
    summary = cashback(app)

    issued = app.get("me/qr/")
    assert issued.status_code == 200, issued.content
    token = issued.json()["token"]
    assert token.startswith("GD1:") and len(token) == 26
    assert (issued.json()["ttl_seconds"], issued.json()["refresh_in_seconds"]) == (60, 50)

    def resolve(value: str) -> Any:
        with world.commit():
            return pos_client.post(
                f"{API}pos/cashback/resolve-qr/", {"token": value, "sale_ref": "CHK-501"}, format="json"
            )

    scanned = resolve(token)
    assert scanned.status_code == 200, scanned.content
    body = scanned.json()
    assert set(body) == {"customer_ref", "phone", "phone_mask", "available", "percent", "resolved_at"}
    assert (body["phone"], body["phone_mask"], body["available"]) == (
        phone,
        mask(phone),
        summary["available"],
    )
    assert body["percent"] == summary["tier"]["percent"] == "1.00"
    assert body["customer_ref"].startswith("c_") and len(body["customer_ref"]) == 18
    second = error(resolve(token), 409, "qr_invalid")
    assert (
        second["message"]
        == "QR kod yaroqsiz yoki muddati o‘tgan. Mijozdan ilovada QR kodni yangilashni so‘rang."
    )
    assert AuditEntry.objects.filter(action="pos.qr_resolved").count() == 1
    assert (AuditEntry.objects.get(action="pos.qr_resolved").after or {})["customer_id"] == customer_id
    assert Purchase.objects.filter(customer_id=customer_id).count() == 0 and len(ledger(app)) == 1
    rescanned = resolve(app.get("me/qr/").json()["token"]).json()
    assert rescanned["customer_ref"] == body["customer_ref"]  # stable per (POS device, customer)

    # an unpaid order holding cashback (P2 checkout): the cashier sees what the card shows as available
    add_to_cart(app, stocked(80_000, 2), 1)
    checkout(app, provider="click", cashback=10_000)
    holding = cashback(app)
    assert (holding["held"], holding["available"]) == (10_000, 38_600)
    assert resolve(app.get("me/qr/").json()["token"]).json()["available"] == holding["available"]

    # logout revokes the outstanding tokens (P1 logout → P0 qr_store → P3 resolve)
    pending = app.get("me/qr/").json()["token"]
    assert app.post("auth/logout/", {"refresh": app.refresh}, auth=False).status_code == 204
    error(resolve(pending), 409, "qr_invalid")

    # B-W1: a sign-in on another install leaves this session (and its token) alone; signing this session
    # out from the other device's session list revokes it
    back = app.post("auth/login/", {"phone": phone, "password": PASSWORD}, auth=False)
    assert back.status_code == 200 and back.json()["device_key"] is None
    app.store(back.json())
    left_behind = app.get("me/qr/").json()["token"]
    other = world.app("fid-journey5-other")
    assert (
        other.post("auth/login/", {"phone": phone, "password": PASSWORD}, auth=False).json()["next_step"]
        == "verify_device"
    )
    code = verify(other, phone, "login_new_device")
    signed = other.post(
        "auth/login/", {"phone": phone, "password": PASSWORD, "verification_token": code}, auth=False
    )
    assert signed.status_code == 200, signed.content
    other.store(signed.json())
    kept = app.get("me/qr/").json()["token"]
    assert resolve(left_behind).status_code == 200
    with world.commit():
        assert other.post("me/sessions/revoke-others/").json() == {"revoked": 1}
    error(resolve(kept), 409, "qr_invalid")
    outbox_clean()


@pytest.mark.django_db(transaction=True)
def test_journey_6_booking_race_confirm_push_ack_and_delivered_counter(
    monkeypatch: pytest.MonkeyPatch, fcm: FakeFcm, staff_client: APIClient, clock: Clock
) -> None:
    """C§2.11, §2.12, §2.14.2: two customers book the same slot concurrently → one 201, one 409
    ``slot_taken``; staff confirm → ``push.appointment`` inbox row whose push carries ``inbox_id`` → ack; a
    campaign to the same customer → ack → the admin ``delivered`` counter counts it once."""
    publish_terms(TERMS_V1)
    world = World(nullcontext, fcm, Staff(staff_client, nullcontext), clock)
    day = booking_day()
    link = specialist(day)
    apps = [world.app("fid-journey6-alice"), world.app("fid-journey6-bobur")]
    phones = ["+998901230061", "+998901230062"]
    for app, phone in zip(apps, phones, strict=True):
        sign_up(app, phone)
        register_push(app, f"fcm-{app.fid}")
    free = apps[0].get(
        f"specialists/{link.professional_id}/slots/", {"date": day.isoformat(), "service_id": link.service_id}
    )
    assert free.status_code == 200 and "10:00" in [s["start"] for s in free.json()["slots"]]

    start = threading.Event()
    barrier = threading.Barrier(2, timeout=10)
    monkeypatch.setattr(appointment_services, "_before_write", lambda: barrier.wait())
    results: dict[int, Any] = {}

    def attempt(index: int) -> None:
        try:
            assert start.wait(timeout=10)
            results[index] = book(apps[index], link, slot(day, 10))
        except Exception as exc:  # noqa: BLE001 - the outcome is the assertion
            results[index] = exc
        finally:
            connection.close()

    threads = [threading.Thread(target=attempt, args=(index,)) for index in range(2)]
    for thread in threads:
        thread.start()
    start.set()
    for thread in threads:
        thread.join(timeout=10)
    monkeypatch.setattr(appointment_services, "_before_write", lambda: None)
    statuses = sorted(getattr(response, "status_code", 0) for response in results.values())
    assert statuses == [201, 409], results
    winner = next(index for index, response in results.items() if response.status_code == 201)
    loser = 1 - winner
    lost = results[loser].json()
    assert (lost["code"], lost["message"], lost["details"]) == (
        "slot_taken",
        "Bu vaqt band bo‘lib qoldi. Boshqa vaqtni tanlang.",
        None,
    )
    appointment = results[winner].json()
    assert (appointment["status"], appointment["status_label"]) == ("requested", "Tasdiqlanishi kutilmoqda")
    assert Appointment.objects.count() == 1
    taken = apps[loser].get(
        f"specialists/{link.professional_id}/slots/", {"date": day.isoformat(), "service_id": link.service_id}
    )
    assert "10:00" not in [s["start"] for s in taken.json()["slots"]]
    assert apps[loser].get("me/appointments/").json()["results"] == []

    # staff confirm → push.appointment → inbox row + push carrying the inbox id → ack
    confirmed = world.staff.post(f"appointments/{appointment['id']}/confirm/")
    assert confirmed.status_code == 200, confirmed.content
    app = apps[winner]
    feed = inbox(app)
    [row] = feed["results"]
    assert (row["kind"], row["title"]) == ("appointment", "Qabul tasdiqlandi")
    assert row["link"] == {
        "kind": "appointment",
        "product_id": None,
        "order_id": None,
        "appointment_id": appointment["id"],
        "url": None,
    }
    assert row["delivered_at"] is None and feed["unread_count"] == 1
    [message] = pushes(world, "appointment")
    assert message.token == f"fcm-{app.fid}"
    assert message.data == {
        "type": "appointment",
        "inbox_id": str(row["id"]),
        "appointment_id": str(appointment["id"]),
        "action": "confirmed",
    }
    assert app.post("me/notifications/ack/", {"inbox_ids": [row["id"]]}).json() == {"acknowledged": 1}
    assert inbox(app)["results"][0]["delivered_at"] is not None
    mine = app.get(f"me/appointments/{appointment['id']}/").json()
    assert (mine["status"], mine["status_label"]) == ("confirmed", "Tasdiqlandi")

    # a campaign to the same customer: its ack fills the admin delivered counter, once
    form = {
        "kind": "general",
        "title": "Yangi xizmat",
        "body": "Kosmetolog qabuli endi ilovada.",
        "audience.type": "customer",
        "audience.phone": phones[winner],
    }
    created = world.staff.post("notifications/", form, multipart=True)
    assert created.status_code == 201, created.content
    campaign_row = next(r for r in inbox(app)["results"] if r["title"] == "Yangi xizmat")
    [campaign_push] = pushes(world, "campaign")
    assert campaign_push.data["inbox_id"] == str(campaign_row["id"])
    assert Campaign.objects.get(pk=created.json()["id"]).delivered is None
    for _ in range(2):  # the app retries its batch: never counted twice
        assert app.post("me/notifications/ack/", {"inbox_ids": [campaign_row["id"], row["id"]]}).json() == {
            "acknowledged": 2
        }
    listed = world.staff.get("notifications/").json()["results"]
    assert [(c["id"], c["delivered"]) for c in listed] == [(created.json()["id"], 1)]
    outbox_clean()


@pytest.mark.django_db
def test_journey_7_delete_account_then_reregister_keeps_balance_behind_the_horizon(
    world: World, pos_client: APIClient
) -> None:
    """C§2.2.3, §2.1.3, A1/M2-A15: a customer with a paid order, an unpaid order holding cashback, a future
    appointment and a QR token deletes the account → the unpaid order is cancelled (hold and stock released),
    the appointment cancelled (``account_deleted``), sessions and QR revoked, PII scrubbed; re-registering the
    phone reactivates the same id with the balance, and the old history stays behind the horizon."""
    phone = "+998901230077"
    world.clock.shift(-timedelta(minutes=5))  # the account's first life, minutes before the new holder
    app = world.app("fid-journey7-phone")
    customer_id = int(sign_up(app, phone)["customer"]["id"])
    register_push(app, "fcm-journey7")
    add_address(app)
    grant_cashback(world, customer_id, 20_000)
    serum, mask_product = stocked(200_000, 3), stocked(90_000, 2)

    # a paid order (cashback credited) …
    add_to_cart(app, serum, 1)
    paid = checkout(app, provider="click")
    paid_link = pay(app, int(paid["id"]))
    assert (
        click_prepare(world, txn=2_875_570_001, ref=paid_link["account_ref"], amount=paid["total"])["error"]
        == 0
    )
    done = click_complete(
        world,
        txn=2_875_570_001,
        ref=paid_link["account_ref"],
        amount=paid["total"],
        prepare_id=paid_link["payment_id"],
    )
    assert done["error"] == 0
    # … an unpaid order holding cashback, a future appointment, a QR token and a cart line
    add_to_cart(app, mask_product, 1)
    unpaid = checkout(app, provider="click", cashback=5_000)
    add_to_cart(app, serum, 1)
    day = booking_day()
    link = specialist(day)
    booked = book(app, link, slot(day, 11))
    assert booked.status_code == 201, booked.content
    qr = app.get("me/qr/").json()["token"]
    before = cashback(app)
    assert before["held"] == 5_000 and available_stock(app, mask_product) == 1
    history = ledger(app)

    wrong = app.delete("me/", {"password": "Wrong-Password-77", "verification_token": None})
    assert wrong.status_code == 400 and wrong.json()["errors"][0]["code"] == "password_incorrect"
    deleted = app.delete("me/", {"password": PASSWORD, "verification_token": None})
    assert deleted.status_code == 204, deleted.content

    # sessions and QR tokens are gone
    error(app.get("me/"), 401, "SESSION_EXPIRED")
    error(app.post("auth/refresh/", {"refresh": app.refresh}, auth=False), 401, "SESSION_EXPIRED")
    with world.commit():
        assert (
            pos_client.post(f"{API}pos/cashback/resolve-qr/", {"token": qr}, format="json").json()["code"]
            == "qr_invalid"
        )
    # the unpaid order cancelled by the customer path, the appointment by the account-deleted path
    order = Order.objects.get(pk=unpaid["id"])
    assert (order.status, order.cancellation_reason) == ("cancelled", "customer_request")
    assert Order.objects.get(pk=paid["id"]).status == "processing"  # paid orders continue
    appointment = Appointment.objects.get(pk=booked.json()["id"])
    assert (appointment.status, appointment.cancelled_by) == ("cancelled", "customer")
    event = AppointmentEvent.objects.filter(appointment=appointment, type="cancelled").get()
    assert event.data == {"by": "customer", "reason": "account_deleted", "push_sent": False}
    assert AuditEntry.objects.get(action="customer.deleted").after == {
        "orders_cancelled": 1,
        "appointments_cancelled": 1,
    }
    # PII scrubbed, devices and keys cleared, the cart emptied; the row and phone kept
    gone = Customer.objects.get(pk=customer_id)
    assert (gone.status, gone.phone, gone.name, gone.password, gone.terms_version) == (
        "deleted",
        phone,
        "",
        "",
        "",
    )
    assert (gone.consent_at, gone.consent_ip, gone.language) == (None, None, "uz")
    assert not Address.objects.filter(customer_id=customer_id).exists()
    assert not CartItem.objects.filter(cart__customer_id=customer_id).exists()
    assert list(
        Device.objects.filter(customer_id=customer_id).values_list("is_active", "fcm_token", "key_hash")
    ) == [(False, "", "")]
    assert world.staff.get(f"customers/{customer_id}/").json()["status"] == "deleted"
    # the slot is free again and the hold and stock are back
    free = app.get(
        f"specialists/{link.professional_id}/slots/",
        {"date": day.isoformat(), "service_id": link.service_id},
        auth=False,
    )
    assert "11:00" in [s["start"] for s in free.json()["slots"]]
    assert available_stock(world.app("fid-journey7-browser"), mask_product) == 2
    # a provider callback for the cancelled order cannot start a payment any more
    assert (
        click_prepare(
            world, txn=2_875_570_002, ref=payment_ref.build(int(unpaid["id"])), amount=unpaid["total"]
        )["error"]
        == -9
    )

    # the same phone registers again: the same row, the balance kept, the old history behind the horizon
    world.clock.release()
    fresh = world.app("fid-journey7-newphone")
    sent = fresh.post("auth/send-code/", {"phone": phone, "purpose": "register"}, auth=False)
    assert sent.status_code == 200, sent.content
    token = verify(fresh, phone, "register")
    again = fresh.post(
        "auth/set-password/",
        {"verification_token": token, "password": PASSWORD, "name": "Dilnoza", "terms_version": TERMS_V1},
        auth=False,
    )
    assert again.status_code == 201, again.content
    assert (again.json()["reactivated"], again.json()["is_new_customer"], again.json()["customer"]["id"]) == (
        True,
        False,
        customer_id,
    )
    fresh.store(again.json())
    me = fresh.get("me/").json()
    assert me["name"] == "Dilnoza" and me["default_address"] is None and me["linked_providers"] == []
    horizon = Customer.objects.get(pk=customer_id).history_from
    assert horizon is not None and me["registered_at"] == iso(horizon)
    kept = cashback(fresh)
    assert (kept["balance"], kept["held"]) == (before["balance"], 0)
    entries = ledger(fresh)
    assert [(e["id"], e["amount"], e["balance_after"]) for e in entries] == [
        (e["id"], e["amount"], e["balance_after"]) for e in history
    ]
    order_linked = [e for e in entries if e["kind"] in ("credit", "debit")]
    assert order_linked and all(
        e["order_id"] is None and e["subtitle"] == "Avvalgi hisob" for e in order_linked
    )
    assert fresh.get("me/orders/").json()["results"] == []
    assert fresh.get(f"me/orders/{paid['id']}/").status_code == 404
    assert fresh.post(f"orders/{paid['id']}/reorder/", idem=True).status_code == 404
    assert fresh.get("me/appointments/", {"scope": "past"}).json()["results"] == []
    assert fresh.get(f"me/appointments/{booked.json()['id']}/").status_code == 404
    feed = inbox(fresh)
    assert (feed["results"], feed["unread_count"]) == ([], 0)
    assert fresh.get("cart/").json()["items"] == []
    with world.commit():  # the cashier sees the kept balance for the new holder's QR
        scanned = pos_client.post(
            f"{API}pos/cashback/resolve-qr/", {"token": fresh.get("me/qr/").json()["token"]}, format="json"
        )
    assert scanned.status_code == 200 and scanned.json()["available"] == kept["available"]
    # the previous holder's pickup order is handed over later: no inbox row, no push for the new holder
    register_push(fresh, "fcm-journey7-newphone")
    moved = world.staff.post(f"orders/{paid['id']}/transition/", {"status": "delivered"})
    assert moved.status_code == 200, moved.content
    assert inbox(fresh)["results"] == []
    assert [
        m.data["status"] for m in pushes(world, "order_status") if m.token == "fcm-journey7-newphone"
    ] == []
    outbox_clean()


@pytest.mark.django_db
def test_journey_7b_delete_waits_for_click_in_flight_then_the_late_capture_is_reversed(world: World) -> None:
    """C§2.2.3 step 1 and §2.13.0: ``DELETE me/`` (P1) refuses while a Click payment is in flight (P2's
    definition, 120 s after Prepare) with nothing written; once the customer left Click's page the deletion
    cancels the order, and when Click's Complete still arrives the capture is accepted and reversed."""
    phone = "+998901230078"
    world.clock.shift(-timedelta(minutes=10))
    app = world.app("fid-journey7b-phone")
    customer_id = int(sign_up(app, phone)["customer"]["id"])
    gel = stocked(95_000, 2)
    add_to_cart(app, gel, 1)
    order = checkout(app, provider="click")
    order_id, total = int(order["id"]), int(order["total"])
    link = pay(app, order_id)
    assert click_prepare(world, txn=2_875_571_001, ref=link["account_ref"], amount=total)["error"] == 0

    busy = error(app.delete("me/", {"password": PASSWORD}), 409, "payment_in_progress")
    assert busy["details"] == {"provider": "click", "order_id": order_id}
    assert busy["message"] == "To‘lov Click ilovasida jarayonda. Uni yakunlang yoki biroz kuting."
    assert app.get("me/").status_code == 200 and Order.objects.get(pk=order_id).status == "awaiting_payment"
    assert not AuditEntry.objects.filter(action="customer.deleted").exists()

    world.clock.shift(-timedelta(minutes=10) + timedelta(seconds=121))  # Click left the in-flight window
    assert app.delete("me/", {"password": PASSWORD}).status_code == 204
    assert (Order.objects.get(pk=order_id).status, Payment.objects.get(pk=link["payment_id"]).error_code) == (
        "cancelled",
        "cancelled",
    )
    late = click_complete(
        world, txn=2_875_571_001, ref=link["account_ref"], amount=total, prepare_id=link["payment_id"]
    )
    assert (late["error"], late["merchant_confirm_id"]) == (0, link["payment_id"])
    assert (AuditEntry.objects.get(action="payment.late_capture").after or {})["case"] == "failed_by_us"
    refund = Refund.objects.get(payment_id=link["payment_id"])
    assert (refund.origin, refund.state, refund.amount) == ("late_capture", "succeeded", total)
    assert Order.objects.get(pk=order_id).status == "cancelled"
    assert Customer.objects.get(pk=customer_id).status == "deleted"
    assert Purchase.objects.filter(customer_id=customer_id).count() == 0
    outbox_clean()


@pytest.mark.django_db(transaction=True)
def test_journey_7c_delete_racing_a_click_capture_keeps_the_paid_order(
    monkeypatch: pytest.MonkeyPatch, fcm: FakeFcm, staff_client: APIClient, clock: Clock
) -> None:
    """C§2.2.3 (lock paragraph) and §2.7.1: ``DELETE me/`` (P1) holds the customer-level lock while Click's
    Complete (P2) holds Order → Payment → LoyaltyAccount and inserts its ``Purchase``. The capture is not
    blocked, commits, and "the delete then sees the order paid": the deletion goes on (paid orders continue),
    no deadlock, no refusal."""
    publish_terms(TERMS_V1)
    seed_policies()
    world = World(nullcontext, fcm, Staff(staff_client, nullcontext), clock)
    clock.shift(-timedelta(minutes=5))
    app = world.app("fid-journey7c-phone")
    customer_id = int(sign_up(app, "+998901230079")["customer"]["id"])
    add_to_cart(app, stocked(130_000, 2), 1)
    order = checkout(app, provider="click")
    order_id, total = int(order["id"]), int(order["total"])
    link = pay(app, order_id)
    assert click_prepare(world, txn=2_875_572_001, ref=link["account_ref"], amount=total)["error"] == 0
    clock.shift(-timedelta(minutes=5) + timedelta(seconds=130))  # open, no longer in flight: DELETE may run

    order_locked, customer_locked, cancelling = threading.Event(), threading.Event(), threading.Event()
    real_case = commerce_services.late_capture_case
    real_inflight = commerce_selectors.customer_inflight_order
    real_cancel = commerce_services.cancel_by_customer

    def capture_case(*args: Any, **kwargs: Any) -> Any:
        order_locked.set()  # the Complete holds the Order lock …
        assert customer_locked.wait(timeout=10)  # … the delete holds the customer lock …
        # … and has listed the unpaid order (bounded wait: a fix may lock the orders while listing them)
        cancelling.wait(timeout=2)
        threading.Event().wait(0.3)  # the delete is now waiting for the Order lock
        return real_case(*args, **kwargs)

    def inflight(customer: int) -> Any:
        result = real_inflight(customer)
        customer_locked.set()  # called right after the customer-level lock was taken
        return result

    def cancel(*args: Any, **kwargs: Any) -> Any:
        cancelling.set()
        return real_cancel(*args, **kwargs)

    monkeypatch.setattr(commerce_services, "late_capture_case", capture_case)
    monkeypatch.setattr(commerce_selectors, "customer_inflight_order", inflight)
    monkeypatch.setattr(commerce_services, "cancel_by_customer", cancel)
    results: dict[str, Any] = {}

    def complete() -> None:
        try:
            params = click_params(
                txn=2_875_572_001,
                ref=link["account_ref"],
                amount=str(total),
                action="1",
                prepare_id=link["payment_id"],
            )
            results["complete"] = APIClient().generic(
                "POST", f"{API}webhooks/click/complete/", urlencode(params), content_type=FORM
            )
        except Exception as exc:  # noqa: BLE001 - the outcome is the assertion
            results["complete"] = exc
        finally:
            connection.close()

    def delete() -> None:
        try:
            assert order_locked.wait(timeout=10)
            results["delete"] = app.delete("me/", {"password": PASSWORD})
        except Exception as exc:  # noqa: BLE001 - the outcome is the assertion
            results["delete"] = exc
        finally:
            connection.close()

    threads = [threading.Thread(target=complete), threading.Thread(target=delete)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=20)
    monkeypatch.setattr(commerce_services, "late_capture_case", real_case)
    monkeypatch.setattr(commerce_selectors, "customer_inflight_order", real_inflight)
    monkeypatch.setattr(commerce_services, "cancel_by_customer", real_cancel)

    captured = results["complete"]
    assert getattr(captured, "status_code", None) == 200, captured
    assert (captured.json()["error"], captured.json()["merchant_confirm_id"]) == (0, link["payment_id"])
    assert Order.objects.get(pk=order_id).status == "processing"
    assert Purchase.objects.filter(order_id=order_id).count() == 1
    deleted = results["delete"]
    assert getattr(deleted, "status_code", None) == 204, getattr(deleted, "content", deleted)
    assert Customer.objects.get(pk=customer_id).status == "deleted"
    assert AuditEntry.objects.get(action="customer.deleted").after == {
        "orders_cancelled": 0,
        "appointments_cancelled": 0,
    }


@pytest.mark.django_db(transaction=True)
def test_journey_7d_delete_racing_a_staff_appointment_action_is_not_aborted(
    monkeypatch: pytest.MonkeyPatch, fcm: FakeFcm, staff_client: APIClient, clock: Clock
) -> None:
    """C§2.2.3 step 3 (integration decision of 2026-10-26): an appointment that stopped being cancellable
    between ``DELETE me/``'s selection (P1) and its row lock (P3) — staff rejected it meanwhile — is skipped
    and not counted; it never aborts the deletion."""
    publish_terms(TERMS_V1)
    world = World(nullcontext, fcm, Staff(staff_client, nullcontext), clock)
    app = world.app("fid-journey7d-phone")
    customer_id = int(sign_up(app, "+998901230076")["customer"]["id"])
    day = booking_day()
    link = specialist(day)
    booked = book(app, link, slot(day, 15))
    assert booked.status_code == 201, booked.content
    appointment_id = int(booked.json()["id"])

    selected, staff_done = threading.Event(), threading.Event()
    real_cancel = appointment_services.cancel_by_customer

    def cancel(*args: Any, **kwargs: Any) -> Any:
        selected.set()  # the delete selected the open appointment and holds the customer-level lock …
        staff_done.wait(timeout=10)  # … while staff reject it (they lock the appointment row only)
        return real_cancel(*args, **kwargs)

    monkeypatch.setattr(appointment_services, "cancel_by_customer", cancel)
    results: dict[str, Any] = {}

    def staff_reject() -> None:
        try:
            assert selected.wait(timeout=10)
            results["reject"] = world.staff.post(f"appointments/{appointment_id}/reject/")
        except Exception as exc:  # noqa: BLE001 - the outcome is the assertion
            results["reject"] = exc
        finally:
            staff_done.set()
            connection.close()

    def delete() -> None:
        try:
            results["delete"] = app.delete("me/", {"password": PASSWORD})
        except Exception as exc:  # noqa: BLE001 - the outcome is the assertion
            results["delete"] = exc
        finally:
            connection.close()

    threads = [threading.Thread(target=staff_reject), threading.Thread(target=delete)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=20)
    monkeypatch.setattr(appointment_services, "cancel_by_customer", real_cancel)

    assert getattr(results["reject"], "status_code", None) == 200, results["reject"]
    assert Appointment.objects.get(pk=appointment_id).status == "rejected"  # the staff decision stands
    deleted = results["delete"]
    assert getattr(deleted, "status_code", None) == 204, getattr(deleted, "content", deleted)
    assert Customer.objects.get(pk=customer_id).status == "deleted"
    assert AuditEntry.objects.get(action="customer.deleted").after == {
        "orders_cancelled": 0,
        "appointments_cancelled": 0,
    }


@pytest.mark.django_db
def test_journey_8_checkout_and_booking_refuse_stale_consent_alike(world: World) -> None:
    """C§2.2.4, §1.6, §6.10: after staff publish new terms, checkout (P2) and booking (P3) refuse the old
    consent with the same 409 ``terms_update_required`` body (code, localized message, ``details``), in uz and
    ru; ``me/consent/`` with the current version unblocks both."""
    phone = "+998901230088"
    app = world.app("fid-journey8-phone")
    sign_up(app, phone)
    product = stocked(150_000, 5)
    add_to_cart(app, product, 1)
    day = booking_day()
    link = specialist(day)
    quote = app.post("delivery/calculate/", {"fulfilment": "pickup"}).json()
    body = {
        "fulfilment": "pickup",
        "address_id": None,
        "payment_provider": "click",
        "expected_total": quote["expected_total"],
    }

    publish_terms(TERMS_V2)
    assert app.get("settings/public/", auth=False).json()["terms_version"] == TERMS_V2
    consent = app.get("me/").json()["consent"]
    assert (consent["current_version"], consent["update_required"]) == (TERMS_V2, True)
    for lang, message in (
        (None, "Foydalanish shartlarining yangi tahririni qabul qiling."),
        ("ru", "Примите новую редакцию условий использования."),
    ):
        app.lang = lang
        refused_checkout = error(app.post("orders/checkout/", body, idem=True), 409, "terms_update_required")
        refused_booking = error(book(app, link, slot(day, 10)), 409, "terms_update_required")
        assert without_request_id(refused_checkout) == without_request_id(refused_booking)
        assert refused_checkout["message"] == message
        assert refused_checkout["details"] == {"current_version": TERMS_V2}
    app.lang = None
    assert Order.objects.count() == 0 and Appointment.objects.count() == 0

    stale = app.post("me/consent/", {"terms_version": TERMS_V1})
    assert stale.status_code == 400 and stale.json()["errors"][0]["code"] == "terms_outdated"
    accepted = app.post("me/consent/", {"terms_version": TERMS_V2})
    assert accepted.status_code == 200 and accepted.json()["consent"]["update_required"] is False
    assert app.post("orders/checkout/", body, idem=True).status_code == 201
    assert book(app, link, slot(day, 10)).status_code == 201
    outbox_clean()


@pytest.mark.django_db
def test_journey_8b_unpublished_terms_change_blocks_neither_checkout_nor_booking(world: World) -> None:
    """C§2.4 (``terms_version`` is null while the terms are not published), §2.2.1/§2.2.4: the one consent
    rule compares the customer's version with the **published** terms. Staff typing a new version before both
    uz texts exist publishes nothing: ``settings/public/`` and ``Me`` require no update, ``me/consent/``
    refuses the unpublished version — so neither checkout (P2) nor booking (P3) may answer 409
    ``terms_update_required`` (the app could never satisfy it)."""
    phone = "+998901230089"
    app = world.app("fid-journey8b-phone")
    sign_up(app, phone)
    product = stocked(150_000, 5)
    add_to_cart(app, product, 1)
    day = booking_day()
    link = specialist(day)
    quote = app.post("delivery/calculate/", {"fulfilment": "pickup"}).json()
    body = {
        "fulfilment": "pickup",
        "address_id": None,
        "payment_provider": "click",
        "expected_total": quote["expected_total"],
    }

    publish_terms(TERMS_V2, privacy_uz="")  # a draft: the privacy text is still empty
    assert app.get("settings/public/", auth=False).json()["terms_version"] is None
    assert app.get("me/").json()["consent"]["update_required"] is False
    assert (
        app.post("me/consent/", {"terms_version": TERMS_V2}).json()["errors"][0]["code"] == "terms_outdated"
    )
    booked = book(app, link, slot(day, 10))
    placed = app.post("orders/checkout/", body, idem=True)
    assert (placed.status_code, booked.status_code) == (201, 201), (placed.content, booked.content)


@pytest.mark.django_db
def test_journey_8c_blank_terms_text_is_unpublished_for_booking_too(world: World) -> None:
    """C§2.1.3 publication rule ("the uz texts non-empty") as P1 and ``settings/public/`` apply it (trimmed):
    a whitespace-only privacy text is not published, so booking must not demand a consent that ``me/consent/``
    refuses."""
    phone = "+998901230090"
    app = world.app("fid-journey8c-phone")
    sign_up(app, phone)
    day = booking_day()
    link = specialist(day)
    publish_terms(TERMS_V2, privacy_uz="   ")
    assert app.get("settings/public/", auth=False).json()["terms_version"] is None
    assert app.get("me/").json()["consent"]["update_required"] is False
    assert (
        app.post("me/consent/", {"terms_version": TERMS_V2}).json()["errors"][0]["code"] == "terms_outdated"
    )
    booked = book(app, link, slot(day, 10))
    assert booked.status_code == 201, booked.content


@pytest.mark.django_db
def test_journey_9_delivery_tracking_pushes_in_the_customer_language(world: World) -> None:
    """C§2.13.3, §4.1–4.3, §2.12, §2.2.2: an English-speaking customer's paid delivery order; BTS notifies a
    status, the webhook re-tracks with **our** tracking number (the payload's status is not trusted) and moves
    the order to ``handed_to_courier``; the customer switches to Russian (P1); staff mark the order delivered.
    Every step is a ``push.order_status`` whose inbox title is the customer label in the customer's language
    at that moment (P3), earlier rows keep theirs, and the order labels follow the customer's language."""
    phone = "+998901230099"
    app = world.app("fid-journey9-phone")
    sign_up(app, phone, language="en")
    register_push(app, "fcm-journey9")
    address_id = add_address(app)
    lotion = stocked(210_000, 4)
    add_to_cart(app, lotion, 1)
    with override_settings(FAKE_SCENARIO_HEADER_ENABLED=True):
        scenario = {"HTTP_X_GD_FAKE_SCENARIO": json.dumps({"bts": {"track_status": "200"}})}
        order = checkout(app, provider="click", address_id=address_id, extra=scenario)
    order_id, total = int(order["id"]), int(order["total"])
    link = pay(app, order_id)
    assert click_prepare(world, txn=2_875_590_001, ref=link["account_ref"], amount=total)["error"] == 0
    done = click_complete(
        world, txn=2_875_590_001, ref=link["account_ref"], amount=total, prepare_id=link["payment_id"]
    )
    assert done["error"] == 0
    paid = order_detail(app, order_id)
    assert (paid["status"], paid["status_label"], paid["fulfilment_label"]) == (
        "processing",
        "Being prepared",
        "Delivery",
    )
    tracking = paid["shipment"]["tracking_number"]

    with world.commit():
        notified = APIClient().post(
            f"{API}webhooks/bts/",
            {
                "clientId": order_id,
                "trackingNumber": tracking,
                "status": "300",
                "statusTime": "2026-10-26 15:00:00",
            },
            format="json",
            HTTP_X_GD_WEBHOOK_TOKEN=settings.BTS_WEBHOOK_SECRET,
        )
    assert notified.status_code == 200 and notified.json() == {"ok": True}
    courier = order_detail(app, order_id)
    assert (courier["status"], courier["status_label"]) == ("handed_to_courier", "With the courier")
    assert (
        courier["timeline"][0]["type"],
        courier["timeline"][0]["title"],
        courier["timeline"][0]["subtitle"],
    ) == (
        "handed_to_courier",
        "With the courier",
        "BTS",
    )
    assert [(p["step"], p["state"]) for p in courier["progress"]] == [
        ("placed", "done"),
        ("paid", "done"),
        ("with_courier", "done"),
        ("delivered", "current"),
    ]

    # the customer switches the app to Russian (P1): later pushes and labels follow, earlier rows keep English
    switched = app.call("patch", "me/", {"language": "ru"})
    assert switched.status_code == 200 and switched.json()["language"] == "ru"
    moved = world.staff.post(f"orders/{order_id}/transition/", {"status": "delivered"})
    assert moved.status_code == 200, moved.content
    delivered = order_detail(app, order_id)
    assert (delivered["status"], delivered["status_label"]) == ("delivered", "Доставлен")
    assert (delivered["timeline"][0]["type"], delivered["timeline"][0]["title"]) == ("delivered", "Доставлен")
    assert all(p["state"] == "done" for p in delivered["progress"])
    assert delivered["actions"] == {"pay": False, "cancel": False, "reorder": True}
    feed = inbox(app)["results"]
    assert [(row["title"], row["body"]) for row in feed] == [
        ("Доставлен", f"Заказ №{order_id} доставлен. Спасибо за покупку."),
        ("With the courier", f"Order №{order_id} has been handed to the courier. Track it in the app."),
        ("Being prepared", f"Payment received. Order №{order_id} is being prepared."),
    ]
    sent = pushes(world, "order_status")
    assert [(m.title, m.data["status"], m.data["inbox_id"]) for m in sent] == [
        ("Being prepared", "processing", str(feed[2]["id"])),
        ("With the courier", "handed_to_courier", str(feed[1]["id"])),
        ("Доставлен", "delivered", str(feed[0]["id"])),
    ]
    assert [o["id"] for o in app.get("me/orders/", {"scope": "completed"}).json()["results"]] == [order_id]
    assert app.get("me/orders/", {"scope": "active"}).json()["results"] == []
    outbox_clean()


@pytest.mark.django_db
def test_journey_10_account_security_notices_reach_the_replaced_device(world: World) -> None:
    """C§2.2.8, §2.1.5, §2.14.2, §2.12: a settings link (P1, password re-authentication) and a password
    reset on another install (P1) produce ``push.account_security`` payloads that P3 turns into ``general``
    inbox rows (``link: none``) and pushes; the reset's farewell notice reaches the previous install's token
    with the same ``inbox_id``, which then answers 401 ``password_changed``; the reset unlinks the
    identity."""
    phone = "+998901230100"
    first = world.app("fid-journey10-first")
    customer_id = int(sign_up(first, phone)["customer"]["id"])
    register_push(first, "fcm-journey10-first")
    nonce = first.get("auth/social/nonce/", auth=False).json()["nonce"]
    linked = first.post(
        "me/social/",
        {
            "provider": "google",
            "token": "fake:sub-journey10:malika@gmail.com",
            "token_type": "id_token",
            "nonce": nonce,
            "password": PASSWORD,
        },
        idem=True,
    )
    assert linked.status_code == 200, linked.content
    assert linked.json()["linked_providers"] == ["google"]
    [notice] = inbox(first)["results"]
    assert (notice["kind"], notice["title"]) == ("general", "Hisobingizga yangi kirish usuli qo‘shildi")
    assert (
        notice["body"] == "Hisobingizga Google profili bog‘landi. Agar bu siz bo‘lmasangiz, parolni tiklang."
    )
    assert notice["link"] == {
        "kind": "none",
        "product_id": None,
        "order_id": None,
        "appointment_id": None,
        "url": None,
    }
    [link_push] = pushes(world, "account_security")
    assert link_push.token == "fcm-journey10-first"
    assert link_push.data == {
        "type": "account_security",
        "inbox_id": str(notice["id"]),
        "event": "social_linked",
        "provider": "google",
    }

    second = world.app("fid-journey10-second", platform="android")
    sent = second.post("auth/send-code/", {"phone": phone, "purpose": "reset_password"}, auth=False)
    assert sent.status_code == 200, sent.content
    token = verify(second, phone, "reset_password")
    reset = second.post(
        "auth/reset-password/",
        {"verification_token": token, "password": "Juniper-Ocean!Bright58"},
        auth=False,
    )
    assert reset.status_code == 200, reset.content
    assert len(reset.json()["device_key"]) == 43 and reset.json()["customer"]["linked_providers"] == []
    second.store(reset.json())

    changed = error(first.get("me/"), 401, "password_changed")
    assert (
        changed["message"]
        == "Parolingiz o‘zgartirildi. Qayta kiring. Bu siz bo‘lmasangiz, sotuvchi bilan bog‘laning."
    )
    feed = inbox(second)["results"]
    assert [row["title"] for row in feed] == [
        "Parol o‘zgartirildi",
        "Hisobingizga yangi kirish usuli qo‘shildi",
    ]
    farewell = [m for m in pushes(world, "account_security") if m.data["event"] == "password_reset"]
    assert [(m.token, m.title, m.data["inbox_id"]) for m in farewell] == [
        ("fcm-journey10-first", "Parol o‘zgartirildi", str(feed[0]["id"]))
    ]
    stored = OutboxMessage.objects.get(topic="push.account_security", payload__event="password_reset")
    assert stored.payload["farewell_tokens"] == [] and stored.payload["customer_id"] == customer_id
    unlinked = AuditEntry.objects.get(action="customer.social_unlinked")
    assert unlinked.after == {"provider": "google", "reason": "password_reset"}
    outbox_clean()
