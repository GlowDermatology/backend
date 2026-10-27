# ruff: noqa: S105, S106 - test credentials
"""Follow-up of the P1 review: an optional ``Idempotency-Key`` is honoured on every P1 **C** write that does
not require one (customer contract §1.7, admin §1.7): a retry with the same body replays the stored 2xx
answer with ``Idempotent-Replayed: true``, another body answers 422 ``idempotency_key_reused``, a refusal
stores nothing, and what a refusal deliberately keeps (the re-authentication guard counters) survives the
rolled back transaction."""

from __future__ import annotations

import uuid
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from io import BytesIO
from typing import Any

import pytest
from django.core.cache import cache
from django.core.files.uploadedfile import SimpleUploadedFile
from PIL import Image

from glowderma.accounts import services as account_services
from glowderma.accounts.models import Address, AuthSession, Customer, CustomerSocialIdentity, Device
from glowderma.accounts.tests.factories import CustomerSocialIdentityFactory
from glowderma.core.models import IdempotencyRecord
from glowderma.customers.services import LOGIN_SCOPE, REAUTH_SCOPE, phone_guard_key
from glowderma.customers.tests.p1_support import (
    FID,
    OTHER_FID,
    PASSWORD,
    TERMS,
    Signed,
    password_customer,
    publish_terms,
    reset_fakes,
)

pytestmark = pytest.mark.django_db
ME = "/api/v1/me/"
WRONG = "Wrong-Password-77"


@pytest.fixture(autouse=True)
def fakes() -> Iterator[None]:
    reset_fakes()
    yield
    reset_fakes()


def key() -> dict[str, Any]:
    return {"HTTP_IDEMPOTENCY_KEY": str(uuid.uuid4())}


def png(colour: str) -> SimpleUploadedFile:
    output = BytesIO()
    Image.new("RGB", (20, 10), colour).save(output, format="PNG")
    return SimpleUploadedFile("me.png", output.getvalue(), content_type="image/png")


@dataclass
class Route:
    """One optional-key write: ``body()`` / ``other()`` build the request data afresh (uploads are read
    once), ``check`` asserts the state the first request left, unchanged by the refused second body."""

    method: str
    path: str
    body: Callable[[], Any]
    other: Callable[[], Any]
    check: Callable[[], None]
    multipart: bool = False


def route(name: str, signed: Signed) -> Route:
    customer = signed.customer
    if name == "patch_me":
        return Route(
            "patch",
            ME,
            lambda: {"name": "Aziza"},
            lambda: {"name": "Boshqa"},
            lambda: assert_equal(Customer.objects.get(pk=customer.pk).name, "Aziza"),
        )
    if name == "consent":
        publish_terms()
        return Route(
            "post",
            ME + "consent/",
            lambda: {"terms_version": TERMS},
            lambda: {"terms_version": "2020-01-01"},
            lambda: assert_equal(Customer.objects.get(pk=customer.pk).terms_version, TERMS),
        )
    if name == "image_put":
        return Route(
            "put",
            ME + "image/",
            lambda: {"file": png("#c0ffee")},
            lambda: {"file": png("#123456")},
            lambda: assert_equal(bool(Customer.objects.get(pk=customer.pk).image), True),
            multipart=True,
        )
    if name == "image_delete":
        Customer.objects.filter(pk=customer.pk).update(image=f"customers/{customer.pk}/old.png")
        return Route(
            "delete",
            ME + "image/",
            dict,
            lambda: {"unexpected": True},
            lambda: assert_equal(Customer.objects.get(pk=customer.pk).image.name, ""),
        )
    if name == "device_put":
        return Route(
            "put",
            ME + "device/",
            lambda: {"fcm_token": "fcm-idem-0001", "push_enabled": True, "model": "iPhone 15"},
            lambda: {"fcm_token": "fcm-idem-0002", "push_enabled": True, "model": "iPhone 15"},
            lambda: assert_equal(Device.objects.get(customer=customer, fid=FID).fcm_token, "fcm-idem-0001"),
        )
    if name == "social_unlink":
        CustomerSocialIdentityFactory.create(customer=customer, provider="google", subject="g-idem-1")
        return Route(
            "delete",
            ME + "social/google/",
            lambda: {"password": PASSWORD},
            lambda: {"password": WRONG},
            lambda: assert_equal(CustomerSocialIdentity.objects.filter(customer=customer).exists(), False),
        )
    address = account_services.add_address(customer.pk, text="Toshkent, Chilonzor 9")
    other = account_services.add_address(customer.pk, text="Toshkent, Sergeli 3")
    if name == "address_patch":
        return Route(
            "patch",
            ME + f"addresses/{address.pk}/",
            lambda: {"comment": "3-podyezd"},
            lambda: {"comment": "4-podyezd"},
            lambda: assert_equal(Address.objects.get(pk=address.pk).comment, "3-podyezd"),
        )
    if name == "address_delete":
        return Route(
            "delete",
            ME + f"addresses/{address.pk}/",
            dict,
            lambda: {"unexpected": True},
            lambda: assert_equal(Address.objects.filter(pk=address.pk).exists(), False),
        )
    assert name == "address_default"
    return Route(
        "post",
        ME + f"addresses/{other.pk}/default/",
        dict,
        lambda: {"unexpected": True},
        lambda: assert_equal(Address.objects.get(pk=other.pk).is_default, True),
    )


def assert_equal(actual: Any, expected: Any) -> None:
    assert actual == expected


def send(signed: Signed, spec: Route, data: Any, headers: dict[str, Any]) -> Any:
    call = getattr(signed.client, spec.method)
    if spec.multipart:
        return call(spec.path, data, format="multipart", **headers)
    return call(spec.path, data, **headers)


ROUTES = [
    "patch_me",
    "consent",
    "image_put",
    "image_delete",
    "device_put",
    "social_unlink",
    "address_patch",
    "address_delete",
    "address_default",
]


@pytest.mark.parametrize("name", ROUTES)
def test_an_optional_key_replays_the_stored_answer_and_refuses_another_body(name: str) -> None:
    signed = Signed(password_customer("+998907772001"))
    spec = route(name, signed)
    headers = key()
    first = send(signed, spec, spec.body(), headers)
    assert 200 <= first.status_code < 300, first.data
    assert "Idempotent-Replayed" not in first
    replay = send(signed, spec, spec.body(), headers)
    assert (replay.status_code, replay.data) == (first.status_code, first.data)
    assert replay["Idempotent-Replayed"] == "true"
    reused = send(signed, spec, spec.other(), headers)
    assert (reused.status_code, reused.data["code"]) == (422, "idempotency_key_reused")
    spec.check()
    record = IdempotencyRecord.objects.get(audience="customer", actor_id=signed.customer.pk)
    assert (record.method, record.path, record.response_status) == (
        spec.method.upper(),
        spec.path,
        first.status_code,
    )


def test_a_replay_does_not_run_the_write_again() -> None:
    signed = Signed(password_customer("+998907772002"))
    headers = key()
    first = signed.client.patch(ME, {"name": "Aziza"}, **headers)
    assert first.status_code == 200 and first.data["name"] == "Aziza"
    assert signed.client.patch(ME, {"name": "Boshqa"}).status_code == 200  # another intent, no key
    replay = signed.client.patch(ME, {"name": "Aziza"}, **headers)
    assert replay["Idempotent-Replayed"] == "true" and replay.data["name"] == "Aziza"
    assert Customer.objects.get(pk=signed.customer.pk).name == "Boshqa"


def test_a_refusal_is_not_stored_and_its_retry_runs_again() -> None:
    signed = Signed(password_customer("+998907772003"))
    headers = key()
    refused = signed.client.put(ME + "device/", {"fcm_token": "", "push_enabled": True}, **headers)
    assert refused.data["errors"][0]["code"] == "push_token_missing"
    assert not IdempotencyRecord.objects.exists()
    retried = signed.client.put(ME + "device/", {"fcm_token": "", "push_enabled": True}, **headers)
    assert retried.status_code == 400 and "Idempotent-Replayed" not in retried


def failures(scope: str, ident: str) -> Any:
    return cache.get(f"auth:fail:{scope}:{ident}")


@pytest.mark.parametrize("path", [ME, ME + "social/google/"])
def test_reauthentication_failures_survive_the_rolled_back_transaction(path: str) -> None:
    """``DELETE me/`` and ``DELETE me/social/{provider}/`` refuse a wrong password under the key's
    transaction, which rolls back; the ``customer_reauth`` and ``customer_login`` failure counts they keep on
    purpose (C§1.8) still add up to the lockout."""
    signed = Signed(password_customer("+998907772004"))
    CustomerSocialIdentityFactory.create(customer=signed.customer, provider="google", subject="g-idem-2")
    for attempt in range(1, 6):
        wrong = signed.client.delete(path, {"password": WRONG}, **key())
        assert wrong.data["errors"][0]["code"] == "password_incorrect", wrong.data
        assert failures(REAUTH_SCOPE, str(signed.customer.pk)) == attempt
        assert failures(LOGIN_SCOPE, phone_guard_key(signed.customer.phone)) == attempt
    locked = signed.client.delete(path, {"password": PASSWORD}, **key())
    assert (locked.status_code, locked.data["code"]) == (429, "throttled")
    assert not IdempotencyRecord.objects.exists()
    assert Customer.objects.get(pk=signed.customer.pk).status == "active"
    assert CustomerSocialIdentity.objects.filter(customer=signed.customer).exists()


def test_delete_me_with_a_key_retries_a_refusal_and_keeps_no_payload() -> None:
    signed = Signed(password_customer("+998907772005"))
    assert signed.client.patch(ME, {"name": "Aziza"}, **key()).status_code == 200
    headers = key()
    refused = signed.client.delete(ME, {"password": WRONG}, **headers)
    assert refused.status_code == 400
    deleted = signed.client.delete(ME, {"password": PASSWORD}, **headers)  # errors are never stored
    assert deleted.status_code == 204 and "Idempotent-Replayed" not in deleted
    assert Customer.objects.get(pk=signed.customer.pk).status == "deleted"
    # the PATCH me/ answer (a Me payload) went with the scrub; DELETE me/ stored no body
    [record] = IdempotencyRecord.objects.filter(audience="customer", actor_id=signed.customer.pk)
    assert (record.method, record.response_status, record.response_body) == ("DELETE", 204, None)
    after = signed.client.delete(ME, {"password": PASSWORD}, **headers)
    assert (after.status_code, after.data["code"]) == (401, "SESSION_EXPIRED")


def test_put_device_with_a_key_signs_the_token_holder_out(
    django_capture_on_commit_callbacks: Any,
) -> None:
    """The token moves under the key's transaction; the holder's session revocation and its tombstone (on
    commit) are part of the stored success, and a replay moves nothing again."""
    holder = Signed(password_customer("+998907772006"), fid=OTHER_FID)
    account_services.register_device(
        holder.customer.pk, OTHER_FID, fcm_token="fcm-idem-held", push_enabled=True
    )
    taker = Signed(password_customer("+998907772007"))
    headers = key()
    body = {"fcm_token": "fcm-idem-held", "push_enabled": True}
    with django_capture_on_commit_callbacks(execute=True):
        moved = taker.client.put(ME + "device/", body, **headers)
    assert moved.status_code == 200, moved.data
    assert AuthSession.objects.get(pk=holder.started.session.pk).revoke_reason == "replaced"
    replaced = holder.client.get(ME)
    assert (replaced.status_code, replaced.data["code"]) == (401, "SESSION_EXPIRED")
    replay = taker.client.put(ME + "device/", body, **headers)
    assert replay["Idempotent-Replayed"] == "true" and replay.data == moved.data
    assert list(Device.objects.filter(fcm_token="fcm-idem-held").values_list("customer_id", flat=True)) == [
        taker.customer.pk
    ]
