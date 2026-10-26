"""State checks under the customer-level lock (customer contract §1.9, §2.2.3 step 5, §2.2.6, §2.7.1).

A sign-in or a device registration that races ``DELETE me/`` (or another sign-in) waits for the customer lock
and then re-reads the customer and the device, so a deleted or replaced account never regains a keyed device,
an open session or a push token, and ``register_device`` never deadlocks with ``start_customer_session``.
"""

# ruff: noqa: S105, S106 - FCM tokens in fixtures are test data, not secrets

import threading
import time
from collections.abc import Callable

import pytest
from django.db import connection, transaction
from django.test.utils import CaptureQueriesContext

from glowderma.accounts import services
from glowderma.accounts.models import AuthSession, Customer, Device
from glowderma.accounts.tests.factories import CustomerFactory, DeviceFactory, start_test_session
from glowderma.core import enums
from glowderma.core.api.exceptions import AuthFailed

pytestmark = pytest.mark.django_db


def open_sessions(customer_id: int) -> int:
    return AuthSession.objects.filter(customer_id=customer_id, revoked_at__isnull=True).count()


def keyed_or_active_devices(customer_id: int) -> int:
    return (
        Device.objects.filter(customer_id=customer_id)
        .exclude(is_active=False, key_hash="", fcm_token="")
        .count()
    )


def wait_for_lock_waiters(count: int, timeout: float = 10.0) -> None:
    """Block until ``count`` other backends of the test database wait on a row lock."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT count(*) FROM pg_stat_activity "
                "WHERE datname = current_database() AND wait_event_type = 'Lock'"
            )
            row = cursor.fetchone()
        if row is not None and row[0] >= count:
            return
        time.sleep(0.02)
    raise AssertionError(f"fewer than {count} lock waiters after {timeout}s")


def run_in_thread(target: Callable[[], object], errors: list[BaseException]) -> threading.Thread:
    def body() -> None:
        try:
            target()
        except BaseException as exc:
            errors.append(exc)
        finally:
            connection.close()

    thread = threading.Thread(target=body)
    thread.start()
    return thread


def codes(errors: list[BaseException]) -> list[object]:
    return [error.get_codes() if isinstance(error, AuthFailed) else repr(error) for error in errors]


# --- start_customer_session -------------------------------------------------------------------------------


def test_the_contract_signature_takes_the_device_facts_positionally() -> None:
    """C§1.9: ``start_customer_session(customer_id, fid, platform, app_version, *, issue_device_key)``."""
    customer = CustomerFactory.create()
    started = services.start_customer_session(
        customer.pk, "fid-positional-1", "ios", "1.0.0", issue_device_key=True
    )
    assert started.session.device is not None and started.session.device.fid == "fid-positional-1"
    with pytest.raises(TypeError):
        services.start_customer_session(  # type: ignore[call-arg]
            customer.pk, "fid-positional-1", "ios", "1.0.0", True
        )


def test_a_deleted_customer_cannot_start_a_session_and_nothing_is_written() -> None:
    customer = CustomerFactory.create()
    start_test_session(customer, fid="fid-deleted-001")
    services.delete_customer(customer.pk)
    with pytest.raises(AuthFailed) as caught:
        services.start_customer_session(customer.pk, "fid-deleted-001", "ios", "1.0.0", issue_device_key=True)
    assert caught.value.get_codes() == "SESSION_EXPIRED"
    with pytest.raises(AuthFailed):
        services.start_customer_session(customer.pk, "fid-new-000001", "ios", "1.0.0", issue_device_key=False)
    customer.refresh_from_db()
    assert customer.status == enums.CustomerStatus.DELETED and customer.last_login_at is None
    assert open_sessions(customer.pk) == 0 and keyed_or_active_devices(customer.pk) == 0
    assert not Device.objects.filter(fid="fid-new-000001").exists()


@pytest.mark.django_db(transaction=True)
def test_a_sign_in_racing_the_deletion_is_refused_after_the_lock() -> None:
    """The deletion holds the customer lock; the sign-in (whose caller already saw ``active``) waits, then
    re-reads the status under the lock and writes nothing."""
    customer = CustomerFactory.create()
    start_test_session(customer, fid="fid-race-del-01")
    locked, release = threading.Event(), threading.Event()
    errors: list[BaseException] = []

    def delete_while_holding_the_lock() -> None:
        with transaction.atomic():
            services.lock_customer(customer.pk)
            locked.set()
            release.wait(10)
            services.delete_customer(customer.pk)

    deleter = run_in_thread(delete_while_holding_the_lock, errors)
    assert locked.wait(10)
    signer_errors: list[BaseException] = []
    signer = run_in_thread(
        lambda: services.start_customer_session(
            customer.pk, "fid-race-del-02", "android", "1.0.0", issue_device_key=True
        ),
        signer_errors,
    )
    wait_for_lock_waiters(1)
    release.set()
    deleter.join(10)
    signer.join(10)
    assert errors == []
    assert codes(signer_errors) == ["SESSION_EXPIRED"]
    stored = Customer.objects.get(pk=customer.pk)
    assert stored.status == enums.CustomerStatus.DELETED and stored.last_login_at is None
    assert open_sessions(customer.pk) == 0 and keyed_or_active_devices(customer.pk) == 0


# --- register_device ----------------------------------------------------------------------------------------


def test_register_device_refuses_a_deleted_customer() -> None:
    customer = CustomerFactory.create()
    start_test_session(customer, fid="fid-regdel-0001")
    services.delete_customer(customer.pk)
    with pytest.raises(AuthFailed) as caught:
        services.register_device(customer.pk, "fid-regdel-0001", fcm_token="tok-late", push_enabled=True)
    assert caught.value.get_codes() == "SESSION_EXPIRED"
    device = Device.objects.get(customer=customer, fid="fid-regdel-0001")
    assert (device.fcm_token, device.push_enabled, device.is_active) == ("", False, False)


def test_register_device_refuses_a_replaced_or_logged_out_device() -> None:
    customer = CustomerFactory.create()
    start_test_session(customer, fid="fid-replaced-01")
    start_test_session(customer, fid="fid-current-001")
    with pytest.raises(AuthFailed) as caught:
        services.register_device(customer.pk, "fid-replaced-01", fcm_token="tok-old", push_enabled=True)
    assert caught.value.get_codes() == "SESSION_REPLACED"
    current = Device.objects.get(customer=customer, fid="fid-current-001")
    session = AuthSession.objects.get(device=current, revoked_at__isnull=True)
    services.revoke_customer_sessions(customer.pk, enums.SessionRevokeReason.LOGOUT)
    Device.objects.filter(pk=current.pk).update(is_active=False)
    with pytest.raises(AuthFailed) as caught:
        services.register_device(customer.pk, "fid-current-001", fcm_token="tok-new", push_enabled=True)
    assert caught.value.get_codes() == "SESSION_EXPIRED"
    session.refresh_from_db()
    assert session.revoke_reason == "logout"
    assert not Device.objects.filter(fcm_token__in=("tok-old", "tok-new")).exists()


def test_register_device_takes_the_customer_lock_first() -> None:
    customer = CustomerFactory.create()
    start_test_session(customer, fid="fid-lockorder-1")
    DeviceFactory.create(fcm_token="tok-held")
    with CaptureQueriesContext(connection) as queries:
        services.register_device(customer.pk, "fid-lockorder-1", fcm_token="tok-held", push_enabled=True)
    locking = [q["sql"] for q in queries.captured_queries if q["sql"].rstrip().endswith("UPDATE")]
    assert 'FROM "accounts_customer"' in locking[0] and "FOR NO KEY UPDATE" in locking[0]
    assert all('FROM "accounts_device"' in sql for sql in locking[1:]) and len(locking) == 2


@pytest.mark.django_db(transaction=True)
def test_register_device_and_a_sign_in_on_another_device_serialize_without_a_deadlock() -> None:
    """The probe of the review: device A (signed in) registers token T, held by the customer's device B,
    while B signs in. Both now queue on the customer lock: whichever runs second sees the other's result
    (no ``deadlock detected``), and the customer ends with one open session and one active device."""
    customer = CustomerFactory.create()
    start_test_session(customer, fid="fid-deadlock-A1")
    DeviceFactory.create(customer=customer, fid="fid-deadlock-B1", fcm_token="tok-shared", is_active=False)
    locked, release = threading.Event(), threading.Event()
    errors: list[BaseException] = []

    def hold_the_lock() -> None:
        with transaction.atomic():
            services.lock_customer(customer.pk)
            locked.set()
            release.wait(10)

    holder = run_in_thread(hold_the_lock, errors)
    assert locked.wait(10)
    racers = [
        run_in_thread(
            lambda: services.register_device(
                customer.pk, "fid-deadlock-A1", fcm_token="tok-shared", push_enabled=True
            ),
            errors,
        ),
        run_in_thread(
            lambda: services.start_customer_session(
                customer.pk, "fid-deadlock-B1", "ios", "1.0.0", issue_device_key=True
            ),
            errors,
        ),
    ]
    wait_for_lock_waiters(2)
    release.set()
    for thread in (holder, *racers):
        thread.join(10)
    # Only the device registration may lose (its device was replaced by the sign-in that ran first).
    assert codes(errors) in ([], ["SESSION_REPLACED"])
    assert open_sessions(customer.pk) == 1
    active = Device.objects.get(customer=customer, is_active=True)
    assert active.fid == "fid-deadlock-B1"
    assert AuthSession.objects.get(customer=customer, revoked_at__isnull=True).device_id == active.pk
    assert Device.objects.filter(fcm_token="tok-shared", is_active=False).count() == 0


@pytest.mark.django_db(transaction=True)
def test_register_device_racing_the_deletion_writes_no_token() -> None:
    customer = CustomerFactory.create()
    start_test_session(customer, fid="fid-regrace-001")
    locked, release = threading.Event(), threading.Event()
    errors: list[BaseException] = []

    def delete_while_holding_the_lock() -> None:
        with transaction.atomic():
            services.lock_customer(customer.pk)
            locked.set()
            release.wait(10)
            services.delete_customer(customer.pk)

    deleter = run_in_thread(delete_while_holding_the_lock, errors)
    assert locked.wait(10)
    register_errors: list[BaseException] = []
    registrar = run_in_thread(
        lambda: services.register_device(
            customer.pk, "fid-regrace-001", fcm_token="tok-after-delete", push_enabled=True
        ),
        register_errors,
    )
    wait_for_lock_waiters(1)
    release.set()
    deleter.join(10)
    registrar.join(10)
    assert errors == []
    assert codes(register_errors) == ["SESSION_EXPIRED"]
    device = Device.objects.get(customer=customer, fid="fid-regrace-001")
    assert (device.fcm_token, device.push_enabled, device.is_active, device.key_hash) == (
        "",
        False,
        False,
        "",
    )
