"""The P0 customer services: sessions, devices, keys, deletion (customer contract §1.9, §2.2.3, §2.2.6)."""

# ruff: noqa: S105, S106 - FCM tokens and hashes in fixtures are test data, not secrets

import threading
from datetime import timedelta
from typing import Any

import pytest
from django.core.cache import cache
from django.db import connection, transaction
from django.test.utils import CaptureQueriesContext
from rest_framework.exceptions import ValidationError

from glowderma.accounts import services, topics
from glowderma.accounts.models import Address, AuthSession, Customer, CustomerSocialIdentity, Device
from glowderma.accounts.tests.factories import (
    CustomerFactory,
    CustomerSocialIdentityFactory,
    DeviceFactory,
    start_test_session,
)
from glowderma.core import enums, qr_store, timeutils
from glowderma.core.models import OutboxMessage
from glowderma.core.pii import pii_ref

pytestmark = pytest.mark.django_db


def device_pk(session: AuthSession) -> int:
    if session.device_id is None:  # pragma: no cover - customer sessions always have a device
        raise AssertionError("customer session without a device")
    return session.device_id


def open_sessions(customer: Customer) -> list[AuthSession]:
    return list(AuthSession.objects.filter(customer=customer, revoked_at__isnull=True))


def test_first_sign_in_creates_device_session_and_key(django_capture_on_commit_callbacks: Any) -> None:
    customer = CustomerFactory.create()
    qr_store.put("q" * 64, customer.pk, ttl=60, max_live=2, session_id="other-session")
    with django_capture_on_commit_callbacks(execute=True):
        started = services.start_customer_session(
            customer.pk, fid="fid-aaaa-0001", platform="android", app_version="1.2.3", issue_device_key=True
        )
    session, device = started.session, Device.objects.get(customer=customer, fid="fid-aaaa-0001")
    assert session.device_id == device.pk and session.audience == "customer"
    assert session.device_label == "android app" and "fid" not in session.device_label
    assert (
        session.refresh_hash == services.hash_secret(started.raw_refresh) and len(started.raw_refresh) == 43
    )
    assert session.absolute_expires_at - session.created_at == timedelta(days=60)
    assert (session.rotation, session.rotation_confirmed) == (0, False)
    assert started.raw_device_key is not None and len(started.raw_device_key) == 43
    assert device.key_hash == services.hash_secret(started.raw_device_key)
    assert device.is_active and device.platform == "android" and device.app_version == "1.2.3"
    assert started.farewell_tokens == []
    customer.refresh_from_db()
    assert customer.last_login_at == session.created_at
    # B-W1: a sign-in leaves the QR keys of the customer's other sessions alone
    assert str(qr_store.take("q" * 64)).startswith(f"{customer.pk}:")


def test_a_second_device_keeps_the_first_signed_in(django_capture_on_commit_callbacks: Any) -> None:
    """B-W1 (customer contract §1.9): any number of phones and browsers at once — a sign-in elsewhere
    revokes nothing, retires no device and sends no ``push.session_replaced``."""
    customer = CustomerFactory.create()
    first = start_test_session(customer, fid="fid-first-001")
    first_device = Device.objects.get(pk=device_pk(first.session))
    Device.objects.filter(pk=first_device.pk).update(fcm_token="tok-first", push_enabled=True)
    qr_store.put("f" * 64, customer.pk, ttl=60, max_live=2, session_id=str(first.session.pk))
    with django_capture_on_commit_callbacks(execute=True):
        second = services.start_customer_session(
            customer.pk, fid="fid-second-02", platform="web", app_version="", issue_device_key=False
        )
    assert second.raw_device_key is None and second.farewell_tokens == []
    first.session.refresh_from_db()
    first_device.refresh_from_db()
    assert first.session.revoked_at is None
    assert first_device.is_active and first_device.push_enabled and first_device.fcm_token == "tok-first"
    assert {session.pk for session in open_sessions(customer)} == {first.session.pk, second.session.pk}
    assert Device.objects.filter(customer=customer, is_active=True).count() == 2
    assert Device.objects.get(pk=device_pk(second.session)).platform == "web"
    assert not OutboxMessage.objects.filter(topic=topics.PUSH_SESSION_REPLACED).exists()
    assert cache.get(f"auth:sid:{first.session.pk}") is None
    assert str(qr_store.take("f" * 64)).startswith(f"{customer.pk}:")


def test_a_sign_in_retires_the_devices_left_without_a_session() -> None:
    """A device whose sessions all ended (a reset elsewhere revoked them) stops receiving pushes at the next
    sign-in, and its token becomes a farewell candidate."""
    customer = CustomerFactory.create()
    first = start_test_session(customer, fid="fid-first-001")
    Device.objects.filter(pk=device_pk(first.session)).update(fcm_token="tok-first", push_enabled=True)
    AuthSession.objects.filter(pk=first.session.pk).update(revoked_at=timeutils.now(), revoke_reason="logout")
    started = services.start_customer_session(
        customer.pk, fid="fid-second-02", platform="ios", app_version="1.0.0", issue_device_key=True
    )
    assert started.farewell_tokens == ["tok-first"]
    first_device = Device.objects.get(pk=device_pk(first.session))
    assert (first_device.is_active, first_device.push_enabled, first_device.fcm_token) == (False, False, "")
    assert first_device.key_hash  # the key hash stays (signing in there again is a known device)


def test_signing_in_again_on_the_same_install_replaces_without_a_push(
    django_capture_on_commit_callbacks: Any,
) -> None:
    customer = CustomerFactory.create()
    first = start_test_session(customer)
    Device.objects.filter(pk=device_pk(first.session)).update(fcm_token="tok-same", push_enabled=True)
    qr_store.put("o" * 64, customer.pk, ttl=60, max_live=2, session_id=str(first.session.pk))
    with django_capture_on_commit_callbacks(execute=True):
        again = start_test_session(customer, issue_device_key=False)
    assert again.session.device_id == first.session.device_id and again.farewell_tokens == []
    first.session.refresh_from_db()
    assert first.session.revoke_reason == "replaced"
    assert cache.get(f"auth:sid:{first.session.pk}") == "revoked:replaced"
    assert qr_store.take("o" * 64) is None  # the old session's QR keys die with it
    assert not OutboxMessage.objects.filter(topic=topics.PUSH_SESSION_REPLACED).exists()
    device = Device.objects.get(pk=device_pk(first.session))
    assert device.fcm_token == "tok-same" and device.is_active
    assert open_sessions(customer) == [again.session]


def test_reactivating_a_device_whose_token_another_account_took_clears_it() -> None:
    customer = CustomerFactory.create()
    device = DeviceFactory.create(customer=customer, fid="fid-old-00001", fcm_token="tok-z", is_active=False)
    DeviceFactory.create(fcm_token="tok-z", push_enabled=True)  # another account's active row
    services.start_customer_session(
        customer.pk, fid="fid-old-00001", platform="ios", app_version="1.0.0", issue_device_key=True
    )
    device.refresh_from_db()
    assert device.is_active and device.fcm_token == "" and not device.push_enabled


@pytest.mark.django_db(transaction=True)
def test_concurrent_sign_ins_keep_one_session_per_device() -> None:
    customer = CustomerFactory.create()
    gate = threading.Event()
    errors: list[BaseException] = []

    def sign_in(fid: str) -> None:
        try:
            gate.wait(10)
            services.start_customer_session(
                customer.pk, fid=fid, platform="ios", app_version="1.0.0", issue_device_key=True
            )
        except BaseException as exc:  # pragma: no cover - surfaced below
            errors.append(exc)
        finally:
            connection.close()

    threads = [threading.Thread(target=sign_in, args=(fid,)) for fid in ("fid-race-0001", "fid-race-0001")]
    threads += [threading.Thread(target=sign_in, args=("fid-race-0002",))]
    for thread in threads:
        thread.start()
    gate.set()
    for thread in threads:
        thread.join(10)
    assert errors == []
    # B-W1: one open session per device (the same install signing in twice keeps the later one), and both
    # devices stay signed in
    sessions = open_sessions(customer)
    assert len(sessions) == 2
    assert len({session.device_id for session in sessions}) == 2
    assert Device.objects.filter(customer=customer, is_active=True).count() == 2


def test_device_is_known_only_with_the_right_key() -> None:
    customer = CustomerFactory.create()
    started = start_test_session(customer, fid="fid-known-001")
    assert started.raw_device_key is not None
    assert services.device_is_known(customer.pk, "fid-known-001", started.raw_device_key)
    assert not services.device_is_known(customer.pk, "fid-known-001", None)  # fid only
    assert not services.device_is_known(customer.pk, "fid-known-001", "x" * 43)  # wrong key
    assert not services.device_is_known(customer.pk, "fid-known-001", started.raw_device_key + "x")
    assert not services.device_is_known(customer.pk, "fid-other-001", started.raw_device_key)
    DeviceFactory.create(customer=customer, fid="fid-nokey-001")
    assert not services.device_is_known(customer.pk, "fid-nokey-001", started.raw_device_key)
    renewed = start_test_session(customer, fid="fid-known-001")
    assert renewed.raw_device_key != started.raw_device_key
    assert not services.device_is_known(customer.pk, "fid-known-001", started.raw_device_key)


def test_register_device_moves_a_token_and_requires_one_for_push() -> None:
    customer = CustomerFactory.create()
    started = start_test_session(customer, fid="fid-mine-0001")
    other = DeviceFactory.create(fcm_token="tok-moving", push_enabled=True)
    Device.objects.filter(pk=device_pk(started.session)).update(fcm_invalidated_at=timeutils.now())
    device = services.register_device(
        customer.pk,
        "fid-mine-0001",
        fcm_token="tok-moving",
        push_enabled=True,
        model="iPhone 15",
        os="iOS 18.1",
        locale="uz-UZ",
        app_version="1.1.0",
    )
    other.refresh_from_db()
    assert (other.fcm_token, other.push_enabled, other.is_active) == ("", False, False)
    assert (device.fcm_token, device.push_enabled, device.model, device.os) == (
        "tok-moving",
        True,
        "iPhone 15",
        "iOS 18.1",
    )
    assert device.fcm_invalidated_at is None and device.app_version == "1.1.0"
    with pytest.raises(ValidationError) as caught:
        services.register_device(customer.pk, "fid-mine-0001", fcm_token="", push_enabled=True)
    detail = caught.value.detail
    assert isinstance(detail, dict)
    assert [error.code for error in detail["push_enabled"]] == ["push_token_missing"]  # type: ignore[union-attr]
    off = services.register_device(customer.pk, "fid-mine-0001", fcm_token=None, push_enabled=False)
    assert (off.fcm_token, off.push_enabled) == ("", False)


def test_invalidate_fcm_token_only_while_the_device_holds_it() -> None:
    device = DeviceFactory.create(fcm_token="tok-dead", push_enabled=True)
    assert not services.invalidate_fcm_token(device.pk, token="tok-newer")
    assert services.invalidate_fcm_token(device.pk, token="tok-dead")
    device.refresh_from_db()
    assert (device.fcm_token, device.push_enabled) == ("", False) and device.fcm_invalidated_at is not None
    assert not services.invalidate_fcm_token(device.pk)


def test_move_history_horizon() -> None:
    customer = CustomerFactory.create()
    instant = timeutils.now() - timedelta(days=1)
    services.move_history_horizon(customer.pk, at=instant)
    customer.refresh_from_db()
    assert customer.history_from == instant


def test_delete_customer_clears_keys_consent_and_queues_apple_revokes(
    django_capture_on_commit_callbacks: Any, settings: Any
) -> None:
    settings.APPLE_BUNDLE_IDS = ["uz.glowderma.app"]
    customer = CustomerFactory.create(language="ru", terms_version="2026-10-01")
    Customer.objects.filter(pk=customer.pk).update(
        consent_at=timeutils.now(), consent_ip="10.0.0.1", last_login_at=timeutils.now()
    )
    started = start_test_session(customer)
    Device.objects.filter(pk=device_pk(started.session)).update(fcm_token="tok-del", push_enabled=True)
    apple = CustomerSocialIdentityFactory.create(
        customer=customer, provider="apple", provider_refresh_token="sealed"
    )
    CustomerSocialIdentityFactory.create(customer=customer, provider="google")
    Address.objects.create(customer=customer, text="Toshkent")
    qr_store.put("d" * 64, customer.pk, ttl=60, max_live=2, session_id=str(started.session.pk))
    with django_capture_on_commit_callbacks(execute=True), CaptureQueriesContext(connection) as queries:
        result = services.delete_customer(customer)
    assert result is customer
    assert any(
        "FOR NO KEY UPDATE" in q["sql"] and "accounts_customer" in q["sql"] for q in queries.captured_queries
    )
    assert not any(
        q["sql"].rstrip().endswith("FOR UPDATE") and 'FROM "accounts_customer"' in q["sql"]
        for q in queries.captured_queries
    )
    assert (customer.status, customer.language, customer.terms_version) == ("deleted", "uz", "")
    assert customer.consent_at is None and customer.consent_ip is None and customer.last_login_at is None
    device = Device.objects.get(pk=device_pk(started.session))
    assert (device.is_active, device.push_enabled, device.fcm_token, device.key_hash) == (
        False,
        False,
        "",
        "",
    )
    started.session.refresh_from_db()
    assert started.session.revoke_reason == "deactivated"
    assert cache.get(f"auth:sid:{started.session.pk}") == "revoked:deactivated"
    assert qr_store.take("d" * 64) is None
    assert not CustomerSocialIdentity.objects.filter(customer=customer).exists()
    revoke = OutboxMessage.objects.get(topic=topics.APPLE_REVOKE)
    # P1: the eager apple.revoke handler ran on commit; "sealed" does not open, so it logged an ERROR and
    # blanked the token in the stored payload (a final outcome).
    assert revoke.payload == {
        "identity_id": apple.pk,
        "customer_id": customer.pk,
        "subject": "",
        "subject_ref": pii_ref(apple.subject),
        "client_id": "uz.glowderma.app",
        "sealed_token": "",
    }
    assert revoke.dedupe_key == f"apple.revoke:{apple.pk}"
    again = services.delete_customer(customer.pk)  # idempotent, by id too
    assert again.pk == customer.pk and OutboxMessage.objects.filter(topic=topics.APPLE_REVOKE).count() == 1


def test_apple_identities_without_a_token_queue_nothing() -> None:
    identity = CustomerSocialIdentityFactory.create(provider="apple")
    assert not services.enqueue_apple_revoke(identity)
    assert services.apple_client_id() == ""


@pytest.mark.django_db(transaction=True)
def test_the_customer_lock_does_not_block_foreign_key_inserts() -> None:
    """``FOR NO KEY UPDATE`` (customer contract §2.7.1): a row referencing the customer (here a device; a
    capture's ``Purchase`` in production) takes ``FOR KEY SHARE`` and does not wait for the lock."""
    customer = CustomerFactory.create()
    locked, inserted, release = threading.Event(), threading.Event(), threading.Event()

    def hold_lock() -> None:
        try:
            with transaction.atomic():
                services.lock_customer(customer.pk)
                locked.set()
                release.wait(10)
        finally:
            connection.close()

    def insert_child() -> None:
        try:
            locked.wait(10)
            with transaction.atomic():
                with connection.cursor() as cursor:
                    cursor.execute("SET LOCAL lock_timeout = '3s'")
                Device.objects.create(customer_id=customer.pk, fid="fid-child-001", platform="ios")
            inserted.set()
        finally:
            connection.close()

    holder, child = threading.Thread(target=hold_lock), threading.Thread(target=insert_child)
    holder.start()
    child.start()
    child.join(10)
    release.set()
    holder.join(10)
    assert inserted.is_set()


def test_tombstone_ttl_covers_both_audiences(settings: Any) -> None:
    settings.STAFF_ACCESS_TTL, settings.CUSTOMER_ACCESS_TTL = 900, 1800
    assert services.tombstone_ttl() == 1860
    assert enums.SessionRevokeReason.REFRESH_REUSED.value == "refresh_reused"


def test_register_device_signs_out_the_token_holder_it_deactivates(
    django_capture_on_commit_callbacks: Any,
) -> None:
    """Review nit (fix pass 2): the holder deactivated by a moved token loses its open session too, so its app
    answers ``SESSION_EXPIRED`` on every route (B-W1), not only on ``PUT me/device/``."""
    holder_customer = CustomerFactory.create()
    holder = start_test_session(holder_customer, fid="fid-hold-0001")
    Device.objects.filter(pk=device_pk(holder.session)).update(fcm_token="tok-shared", push_enabled=True)
    stale_customer = CustomerFactory.create()
    stale = start_test_session(stale_customer, fid="fid-stale-001")
    # An inactive row still holding the token: it loses the token, its customer's session stays open.
    Device.objects.filter(pk=device_pk(stale.session)).update(is_active=False, fcm_token="tok-shared")
    qr_store.put("h" * 64, holder_customer.pk, ttl=60, max_live=2, session_id=str(holder.session.pk))
    qr_store.put("s" * 64, stale_customer.pk, ttl=60, max_live=2, session_id=str(stale.session.pk))
    customer = CustomerFactory.create()
    mine = start_test_session(customer, fid="fid-mine-0002")
    with django_capture_on_commit_callbacks(execute=True):
        services.register_device(customer.pk, "fid-mine-0002", fcm_token="tok-shared", push_enabled=True)
    holder.session.refresh_from_db()
    assert holder.session.revoke_reason == enums.SessionRevokeReason.REPLACED
    assert cache.get(f"auth:sid:{holder.session.pk}") == "revoked:replaced"
    assert qr_store.take("h" * 64) is None
    assert open_sessions(stale_customer) == [stale.session]
    assert str(qr_store.take("s" * 64)).startswith(f"{stale_customer.pk}:")
    assert open_sessions(customer) == [mine.session]
    assert not OutboxMessage.objects.filter(topic=topics.PUSH_SESSION_REPLACED).exists()
    assert set(Device.objects.filter(fcm_token="tok-shared").values_list("customer_id", flat=True)) == {
        customer.pk
    }


def test_register_device_for_an_unknown_fid_keeps_the_fid_out_of_the_error() -> None:
    """Review nit (fix pass 2): the escaped error is logged by the handler, so it carries ``pii_ref(fid)``."""
    customer = CustomerFactory.create()
    with pytest.raises(Device.DoesNotExist) as caught:
        services.register_device(customer.pk, "fid-secret-0001", fcm_token=None, push_enabled=False)
    assert "fid-secret-0001" not in str(caught.value)
    assert pii_ref("fid-secret-0001") in str(caught.value)
