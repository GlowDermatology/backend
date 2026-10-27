# ruff: noqa: S105, S106 - test credentials
"""Fix pass 2 regressions: ``PUT me/device/`` taking another customer's FCM token locks that customer before
any device or session row, so the holder's own flows (logout, ``PATCH me/``, ``DELETE me/``, a reset)
serialize with it instead of deadlocking (customer contract §2.2.6, §2.7.1)."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Iterator
from typing import Any

import pytest
from django.db import connection

from glowderma.accounts import services as account_services
from glowderma.accounts.models import AuthSession, Customer, Device
from glowderma.accounts.tests.factories import start_test_session
from glowderma.core.api.exceptions import RequestInProgress
from glowderma.customers import services
from glowderma.customers.tests.p1_support import (
    NEW_PASSWORD,
    PASSWORD,
    Signed,
    password_customer,
    verification,
)
from glowderma.customers.tests.p1_support import reset_fakes as reset_provider_fakes

WAIT = 10
FID_A = "fid-fix2-aaaa-0001"
FID_B = "fid-fix2-bbbb-0002"
FID_C = "fid-fix2-cccc-0003"
TOKEN = "fcm-fix2-token-shared-0001"


@pytest.fixture(autouse=True)
def fakes() -> Iterator[None]:
    reset_provider_fakes()
    yield
    reset_provider_fakes()


def ctx(fid: str) -> services.DeviceContext:
    return services.DeviceContext(fid=fid, platform="ios", app_version="1.0.0", ip="10.0.0.1", lang="uz")


def run(name: str, fn: Callable[[], Any], out: dict[str, Any]) -> threading.Thread:
    def target() -> None:
        try:
            out[name] = ("ok", fn())
        except BaseException as exc:  # noqa: BLE001 - recorded for the assertion
            out[name] = ("err", exc)
        finally:
            connection.close()

    thread = threading.Thread(target=target, name=name)
    thread.start()
    return thread


def two_customers_sharing_an_install() -> tuple[Customer, Customer, account_services.SessionStart]:
    """B signed in on FID_B holding the push token TOKEN; A signed in on FID_A (another install)."""
    holder = password_customer("+998905551001")
    holder_started = start_test_session(holder, fid=FID_B)
    account_services.register_device(holder.pk, FID_B, fcm_token=TOKEN, push_enabled=True)
    taker = password_customer("+998905551002")
    start_test_session(taker, fid=FID_A)
    return taker, holder, holder_started


def pause_after(
    monkeypatch: pytest.MonkeyPatch, name: str, thread_name: str
) -> tuple[threading.Event, threading.Event]:
    """Patch ``accounts.services.<name>`` so the thread ``thread_name`` waits after it (its locks held)."""
    original = getattr(account_services, name)
    locked, go = threading.Event(), threading.Event()

    def patched(*args: Any, **kwargs: Any) -> Any:
        result = original(*args, **kwargs)
        if threading.current_thread().name == thread_name and not locked.is_set():
            locked.set()
            go.wait(WAIT)
        return result

    monkeypatch.setattr(account_services, name, patched)
    return locked, go


def race(
    holder_flow: Callable[[], Any], locked: threading.Event, go: threading.Event, taker: Customer
) -> dict[str, Any]:
    out: dict[str, Any] = {}
    first = run("holder", holder_flow, out)
    assert locked.wait(WAIT)
    second = run(
        "device",
        lambda: account_services.register_device(taker.pk, FID_A, fcm_token=TOKEN, push_enabled=True),
        out,
    )
    time.sleep(1)  # the PUT reaches its lock wait while the holder's flow holds its locks
    go.set()
    first.join(WAIT + 5)
    second.join(WAIT + 5)
    assert not first.is_alive() and not second.is_alive()
    return out


def assert_token_moved(out: dict[str, Any], taker: Customer) -> None:
    errors = [value for state, value in out.values() if state == "err"]
    assert not errors, out
    assert Device.objects.filter(fcm_token=TOKEN).count() == 1
    assert Device.objects.get(fcm_token=TOKEN).customer_id == taker.pk


@pytest.mark.django_db(transaction=True)
def test_logout_vs_a_foreign_put_device_does_not_deadlock(monkeypatch: pytest.MonkeyPatch) -> None:
    taker, _holder, holder_started = two_customers_sharing_an_install()
    locked, go = pause_after(monkeypatch, "_revoke_open_customer_sessions", "holder")
    out = race(lambda: account_services.logout_customer(holder_started.raw_refresh), locked, go, taker)
    assert_token_moved(out, taker)
    session = AuthSession.objects.get(pk=holder_started.session.pk)
    assert session.revoke_reason == "logout"


@pytest.mark.django_db(transaction=True)
def test_patch_me_push_vs_a_foreign_put_device_does_not_deadlock(monkeypatch: pytest.MonkeyPatch) -> None:
    taker, holder, holder_started = two_customers_sharing_an_install()
    locked, go = pause_after(monkeypatch, "lock_signed_in_customer", "holder")
    out = race(
        lambda: services.update_profile(
            holder.pk, {"push_enabled": False}, fid=FID_B, sid=str(holder_started.session.pk)
        ),
        locked,
        go,
        taker,
    )
    assert_token_moved(out, taker)
    holder_device = Device.objects.get(customer=holder, fid=FID_B)
    assert (holder_device.is_active, holder_device.push_enabled) == (False, False)
    assert AuthSession.objects.get(pk=holder_started.session.pk).revoke_reason == "replaced"


@pytest.mark.django_db(transaction=True)
def test_delete_me_vs_a_foreign_put_device_does_not_deadlock(monkeypatch: pytest.MonkeyPatch) -> None:
    taker, holder, holder_started = two_customers_sharing_an_install()
    locked, go = pause_after(monkeypatch, "lock_signed_in_customer", "holder")
    out = race(
        lambda: services.delete_account(holder.pk, password=PASSWORD, sid=str(holder_started.session.pk)),
        locked,
        go,
        taker,
    )
    assert_token_moved(out, taker)
    holder.refresh_from_db()
    assert holder.status == "deleted"


@pytest.mark.django_db(transaction=True)
def test_reset_vs_a_foreign_put_device_does_not_deadlock(monkeypatch: pytest.MonkeyPatch) -> None:
    """The account-recovery path: the holder's reset from a third install while A's PUT takes the token."""
    taker, holder, _holder_started = two_customers_sharing_an_install()
    locked, go = pause_after(monkeypatch, "sign_out_customer", "holder")
    token = verification(holder.phone, "reset_password")
    out = race(
        lambda: services.reset_password(token, password=NEW_PASSWORD, device=ctx(FID_C)), locked, go, taker
    )
    assert_token_moved(out, taker)
    assert Device.objects.get(customer=holder, fid=FID_C).is_active


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("attempts", [3, 1])
def test_a_token_holder_appearing_after_the_unlocked_read_is_locked_too(
    monkeypatch: pytest.MonkeyPatch, attempts: int
) -> None:
    """A writer that bypasses ``register_device`` (and so its per-token lock) gives the token to another
    install between the unlocked read and the device lock: the attempt starts again with that customer locked
    first and the token still moves to the caller's device. The last attempt never locks a customer after
    the devices: it answers 409 ``request_in_progress`` with nothing written."""
    late = password_customer("+998905551003")
    start_test_session(late, fid=FID_C)
    taker = password_customer("+998905551004")
    start_test_session(taker, fid=FID_A)
    monkeypatch.setattr(account_services, "REGISTER_DEVICE_ATTEMPTS", attempts)
    original = account_services.lock_customer
    calls: list[int] = []
    moved = threading.Event()

    def patched(customer_id: int) -> Customer:
        calls.append(customer_id)
        if not moved.is_set():
            moved.set()

            def give_token() -> None:
                try:
                    Device.objects.filter(customer=late, fid=FID_C).update(fcm_token=TOKEN, push_enabled=True)
                finally:
                    connection.close()

            mover = threading.Thread(target=give_token)
            mover.start()
            mover.join(WAIT)
        return original(customer_id)

    monkeypatch.setattr(account_services, "lock_customer", patched)
    if attempts == 1:
        with pytest.raises(RequestInProgress):
            account_services.register_device(taker.pk, FID_A, fcm_token=TOKEN, push_enabled=True)
        assert calls == [taker.pk]  # the late holder's customer is never locked after the devices
        assert Device.objects.get(customer=taker, fid=FID_A).fcm_token == ""
        late_device = Device.objects.get(customer=late, fid=FID_C)
        assert (late_device.fcm_token, late_device.is_active) == (TOKEN, True)
        return
    device = account_services.register_device(taker.pk, FID_A, fcm_token=TOKEN, push_enabled=True)
    assert device.fcm_token == TOKEN
    late_device = Device.objects.get(customer=late, fid=FID_C)
    assert (late_device.fcm_token, late_device.is_active) == ("", False)
    # the second attempt locked both customers in ascending order before any device row
    assert calls[1:] == sorted({late.pk, taker.pk})


# --- two customers registering the same new token (verification round 2) ---------------------------------


def waiting_lock_seen() -> bool:
    """Whether some backend waits for a lock (the second PUT queued behind the first one's transaction)."""
    deadline = time.monotonic() + WAIT
    with connection.cursor() as cursor:
        while time.monotonic() < deadline:
            cursor.execute("SELECT count(*) FROM pg_locks WHERE NOT granted")
            row = cursor.fetchone()
            if row is not None and row[0]:
                return True
            time.sleep(0.05)
    return False


@pytest.mark.django_db(transaction=True)
def test_two_customers_put_the_same_new_token_concurrently(monkeypatch: pytest.MonkeyPatch) -> None:
    """A and B PUT me/device/ with a token no row holds yet. A pauses after saving the token on its device
    (inside its transaction); B's PUT then waits for A instead of saving the token too and failing on
    ``device_active_fcm_token_unique`` (500). Both answer 200; B, the later one, holds the token and A's
    install is signed out with ``SESSION_EXPIRED`` (customer contract §2.2.6, §1.9; B-W1)."""
    first = Signed(password_customer("+998905551011"), fid=FID_A)
    second = Signed(password_customer("+998905551012"), fid=FID_B)
    original_save = Device.save
    saved, go = threading.Event(), threading.Event()

    def save(self: Device, *args: Any, **kwargs: Any) -> None:
        original_save(self, *args, **kwargs)
        if threading.current_thread().name == "first" and self.fcm_token == TOKEN and not saved.is_set():
            saved.set()
            go.wait(WAIT)

    monkeypatch.setattr(Device, "save", save)
    body = {"fcm_token": TOKEN, "push_enabled": True, "model": "iPhone 15", "os": "iOS 18", "locale": "uz"}
    out: dict[str, Any] = {}
    one = run("first", lambda: first.client.put("/api/v1/me/device/", body), out)
    try:
        assert saved.wait(WAIT)
        two = run("second", lambda: second.client.put("/api/v1/me/device/", body), out)
        assert waiting_lock_seen()
    finally:
        go.set()
    one.join(WAIT + 5)
    two.join(WAIT + 5)
    assert not one.is_alive() and not two.is_alive()
    for name in ("first", "second"):
        state, response = out[name]
        assert state == "ok", response
        assert response.status_code == 200, (name, response.data)
    holders = Device.objects.filter(fcm_token=TOKEN)
    assert [(row.customer_id, row.fid, row.is_active) for row in holders] == [
        (second.customer.pk, FID_B, True)
    ]
    loser = Device.objects.get(customer=first.customer, fid=FID_A)
    assert (loser.fcm_token, loser.push_enabled, loser.is_active) == ("", False, False)
    assert AuthSession.objects.get(pk=first.started.session.pk).revoke_reason == "replaced"
    replaced = first.client.get("/api/v1/me/")
    assert (replaced.status_code, replaced.data["code"]) == (401, "SESSION_EXPIRED")
