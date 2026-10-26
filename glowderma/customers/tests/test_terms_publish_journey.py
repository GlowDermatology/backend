"""Journey (gap X-SET-LEGAL): a fresh install cannot register customers until staff publish the terms through
the admin API; a later publication asks an existing customer to accept again before checkout.

Admin contract §2.11 (``PATCH settings/``, ``POST settings/terms/publish/``) × customer contract §2.1.3
(``auth/set-password/``), §2.2.4 (``me/consent/``, 409 ``terms_update_required`` at checkout) and §2.4
(``settings/public/``). Every step goes through the HTTP routes with the app's device headers and the staff
token; on-commit effects (the public-settings cache generation, the outbox) run as on a real commit.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Iterator
from datetime import timedelta
from typing import Any

import pytest
from django.conf import settings
from django.utils import timezone
from rest_framework.test import APIClient

from glowderma.accounts.models import Customer
from glowderma.catalog.tests.factories import ProductFactory, ProductStockFactory
from glowderma.core.models import AuditEntry
from glowderma.integrations.eskiz.registry import fake_eskiz

pytestmark = pytest.mark.django_db

API = "/api/v1/"
PHONE = "+998901234500"
PASSWORD = "Saffron!Harbor-Long72"  # noqa: S105 - test credential
VERSION = re.compile(r"^\d{8}-\d+$")


@pytest.fixture(autouse=True)
def clean_sms() -> Iterator[None]:
    fake_eskiz().sent.clear()
    yield
    fake_eskiz().sent.clear()


class App:
    """One app install: device headers and the bearer token once signed in."""

    def __init__(self, capture: Any) -> None:
        self.capture = capture
        self.access: str | None = None

    def call(self, method: str, path: str, body: Any = None, *, idem: bool = False) -> Any:
        headers: dict[str, Any] = {
            "HTTP_X_GD_DEVICE_ID": "fid-terms-journey",
            "HTTP_X_GD_PLATFORM": "android",
            "HTTP_X_GD_APP_VERSION": "1.0.0",
        }
        if self.access:
            headers["HTTP_AUTHORIZATION"] = f"Bearer {self.access}"
        if idem:
            headers["HTTP_IDEMPOTENCY_KEY"] = str(uuid.uuid4())
        with self.capture(execute=True):
            if method == "get":
                return APIClient().get(API + path, body, **headers)
            return getattr(APIClient(), method)(API + path, body, format="json", **headers)


class Staff:
    def __init__(self, client: APIClient, capture: Any) -> None:
        self.client = client
        self.capture = capture

    def patch(self, body: dict[str, Any]) -> Any:
        with self.capture(execute=True):
            return self.client.patch(f"{API}admin/settings/", body, format="json")

    def publish(self) -> Any:
        with self.capture(execute=True):
            return self.client.post(
                f"{API}admin/settings/terms/publish/",
                format="json",
                HTTP_IDEMPOTENCY_KEY=str(uuid.uuid4()),
            )


def verification_token(app: App) -> str:
    sent = app.call("post", "auth/send-code/", {"phone": PHONE, "purpose": "register"})
    assert sent.status_code == 200, sent.content
    verified = app.call(
        "post",
        "auth/verify-code/",
        {"phone": PHONE, "purpose": "register", "code": settings.ESKIZ_FAKE_CODE},
    )
    assert verified.status_code == 200, verified.content
    return str(verified.json()["verification_token"])


def register(app: App, token: str, terms_version: str) -> Any:
    return app.call(
        "post",
        "auth/set-password/",
        {
            "verification_token": token,
            "password": PASSWORD,
            "name": "Malika Karimova",
            "terms_version": terms_version,
        },
    )


def checkout(app: App) -> Any:
    quote = app.call(
        "post", "delivery/calculate/", {"fulfilment": "pickup", "address_id": None, "cashback_to_use": 0}
    )
    assert quote.status_code == 200, quote.content
    body = {
        "fulfilment": "pickup",
        "address_id": None,
        "payment_provider": "click",
        "cashback_to_use": 0,
        "expected_total": quote.json()["expected_total"],
        "customer_note": "",
    }
    return app.call("post", "orders/checkout/", body, idem=True)


def test_registration_waits_for_published_terms_and_a_new_version_asks_for_consent_again(
    staff_client: APIClient, django_capture_on_commit_callbacks: Any
) -> None:
    app = App(django_capture_on_commit_callbacks)
    staff = Staff(staff_client, django_capture_on_commit_callbacks)

    # 1. a fresh install: nothing published, registration is closed
    public = app.call("get", "settings/public/").json()
    assert (public["terms_version"], public["terms"], public["privacy_policy"]) == (None, "", "")
    token = verification_token(app)
    refused = register(app, token, "1")
    assert (refused.status_code, refused.json()["code"]) == (503, "terms_not_published")
    assert not Customer.objects.filter(phone=PHONE).exists()

    # 2. staff save the texts: still not published (saving never publishes)
    saved = staff.patch(
        {
            "terms": {"uz": "Foydalanish shartlari (1-tahrir).", "ru": "Условия использования."},
            "privacy_policy": {"uz": "Maxfiylik siyosati."},
            "store_phone": "+998712000001",
            "telegram_url": "https://t.me/glowderma",
        }
    )
    assert saved.status_code == 200, saved.content
    assert saved.json()["terms_version"] is None
    assert register(app, token, "1").json()["code"] == "terms_not_published"

    # 3. staff publish: the app sees the version, registration accepts exactly it
    published = staff.publish()
    assert published.status_code == 200, published.content
    first = published.json()["terms_version"]
    assert VERSION.match(first) and first.endswith("-1")
    public = app.call("get", "settings/public/").json()
    assert public["terms_version"] == first
    assert (public["terms"], public["store_phone"], public["telegram_url"]) == (
        "Foydalanish shartlari (1-tahrir).",
        "+998712000001",
        "https://t.me/glowderma",
    )
    stale = register(app, token, "2026-01-01")
    assert stale.json()["errors"][0]["code"] == "terms_outdated"
    created = register(app, token, first)
    assert created.status_code == 201, created.content
    app.access = created.json()["access"]
    me = app.call("get", "me/").json()
    assert me["consent"]["terms_version"] == first and me["consent"]["update_required"] is False

    # the customer can check out under the version they accepted
    product = ProductFactory.create(price=120_000, billz_price=120_000)
    ProductStockFactory.create(product=product, quantity=5, synced_at=timezone.now() - timedelta(hours=1))
    added = app.call("post", "cart/items/", {"product_id": product.pk, "quantity": 1}, idem=True)
    assert added.status_code == 200, added.content

    # 4. staff change the terms and publish again: the existing customer must accept before checkout
    assert staff.patch({"terms": {"uz": "Foydalanish shartlari (2-tahrir)."}}).status_code == 200
    second = staff.publish().json()["terms_version"]
    assert VERSION.match(second) and second != first
    me = app.call("get", "me/").json()
    assert me["consent"] == {
        "terms_version": first,
        "accepted_at": me["consent"]["accepted_at"],
        "current_version": second,
        "update_required": True,
    }
    blocked = checkout(app)
    assert blocked.status_code == 409, blocked.content
    assert blocked.json()["code"] == "terms_update_required"
    assert blocked.json()["details"] == {"current_version": second}
    outdated = app.call("post", "me/consent/", {"terms_version": first})
    assert outdated.status_code == 400 and outdated.json()["errors"][0]["code"] == "terms_outdated"
    accepted = app.call("post", "me/consent/", {"terms_version": second})
    assert accepted.status_code == 200, accepted.content
    assert accepted.json()["consent"]["update_required"] is False
    placed = checkout(app)
    assert placed.status_code == 201, placed.content
    assert placed.json()["status"] == "awaiting_payment"

    # the audit trail: two publications, the consent, never the texts themselves
    log = AuditEntry.objects.filter(action="settings.terms_published").order_by("id")
    assert [(entry.after or {}).get("to") for entry in log] == [first, second]
    assert AuditEntry.objects.filter(action="customer.consent_accepted").count() == 1
    edits = AuditEntry.objects.filter(action="settings.updated")
    assert all("tahrir" not in str(entry.after) for entry in edits)
