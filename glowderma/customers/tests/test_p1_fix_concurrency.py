# ruff: noqa: S105, S106 - test credentials
"""Fix pass 1 regressions: signed-in writes re-checked under the customer-level lock, one lock order
(Customer → identity / session / device / SmsCode), re-checked credentials and best-effort cleanup after
commit (customer contract §2.7.1, §6.16, §6.17)."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Iterator
from io import BytesIO
from typing import Any

import pytest
from django.core import signing
from django.core.cache import cache
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import connection
from PIL import Image
from rest_framework.exceptions import NotFound, ValidationError

from glowderma.accounts import services as account_services
from glowderma.accounts.models import Address, AuthSession, Customer, CustomerSocialIdentity
from glowderma.accounts.tests.factories import start_test_session
from glowderma.core.api.exceptions import AuthFailed, PasswordChanged
from glowderma.core.models import AuditEntry, OutboxMessage
from glowderma.core.pii import pii_ref
from glowderma.customers import services
from glowderma.customers.tests.p1_support import (
    NEW_PASSWORD,
    PASSWORD,
    TERMS,
    Signed,
    app_client,
    password_customer,
    publish_terms,
    reset_fakes,
    verification,
)
from glowderma.integrations.oauth.registry import fake_apple

ME = "/api/v1/me/"
FID_A = "fid-fix-aaaa-0001"
FID_B = "fid-fix-bbbb-0002"
WAIT = 10


@pytest.fixture(autouse=True)
def fakes() -> Iterator[None]:
    reset_fakes()
    yield
    reset_fakes()


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


def errors(out: dict[str, Any]) -> list[BaseException]:
    return [value for state, value in out.values() if state == "err"]


def png() -> SimpleUploadedFile:
    buffer = BytesIO()
    Image.new("RGB", (32, 32), "red").save(buffer, "PNG")
    return SimpleUploadedFile("a.png", buffer.getvalue(), content_type="image/png")


# --- signed-in writes after a concurrent DELETE me/ or reset (deterministic interleavings) ---------------


@pytest.mark.django_db
def test_link_social_after_a_concurrent_delete_links_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    publish_terms()
    signed = Signed(password_customer("+998907770011"))
    original = services.verify_social_token

    def verify_then_delete(*args: Any, **kwargs: Any) -> Any:
        claims = original(*args, **kwargs)
        account_services.delete_customer(signed.customer.pk)  # DELETE me/ commits here
        return claims

    monkeypatch.setattr(services, "verify_social_token", verify_then_delete)
    body = {"provider": "google", "token": "fake:prev-holder:p@gmail.com", "nonce": "*", "password": PASSWORD}
    response = signed.client.post(ME + "social/", body, **signed.idem())
    assert (response.status_code, response.data["code"]) == (401, "SESSION_EXPIRED")
    assert not CustomerSocialIdentity.objects.filter(subject="prev-holder").exists()


@pytest.mark.django_db
def test_link_social_after_a_concurrent_reset_links_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    signed = Signed(password_customer("+998907770012"))
    original = services.verify_social_token

    def verify_then_reset(*args: Any, **kwargs: Any) -> Any:
        claims = original(*args, **kwargs)
        token = verification(str(signed.customer.phone), "reset_password")
        services.reset_password(token, password=NEW_PASSWORD, device=ctx(FID_B))
        return claims

    monkeypatch.setattr(services, "verify_social_token", verify_then_reset)
    body = {"provider": "google", "token": "fake:taker:t@gmail.com", "nonce": "*", "password": PASSWORD}
    response = signed.client.post(ME + "social/", body, **signed.idem())
    assert (response.status_code, response.data["code"]) == (401, "password_changed")
    assert not CustomerSocialIdentity.objects.filter(subject="taker").exists()


@pytest.mark.django_db(transaction=True)
def test_link_social_vs_delete_thread_race(monkeypatch: pytest.MonkeyPatch) -> None:
    publish_terms()
    customer = password_customer("+998907770022")
    started = start_test_session(customer, fid=FID_A)
    verified, deleted = threading.Event(), threading.Event()
    original = services.verify_social_token

    def slow_verify(*args: Any, **kwargs: Any) -> Any:
        claims = original(*args, **kwargs)
        verified.set()
        assert deleted.wait(WAIT)
        return claims

    monkeypatch.setattr(services, "verify_social_token", slow_verify)
    out: dict[str, Any] = {}
    link = run(
        "link",
        lambda: services.link_social(
            customer.pk,
            "google",
            "fake:race-sub:r@gmail.com",
            None,
            "*",
            None,
            password=PASSWORD,
            verification_token=None,
            fid=FID_A,
            sid=str(started.session.pk),
        ),
        out,
    )

    def delete() -> Any:
        try:
            assert verified.wait(WAIT)
            return services.delete_account(customer.pk, password=PASSWORD, sid=str(started.session.pk))
        finally:
            deleted.set()

    remover = run("delete", delete, out)
    link.join(WAIT + 5)
    remover.join(WAIT + 5)
    assert out["delete"][0] == "ok", out
    assert out["link"][0] == "err" and isinstance(out["link"][1], AuthFailed)
    customer.refresh_from_db()
    assert customer.status == "deleted"
    assert not CustomerSocialIdentity.objects.filter(customer=customer).exists()


@pytest.mark.django_db
@pytest.mark.parametrize("route", ["address", "image", "profile", "password"])
def test_signed_in_writes_after_a_concurrent_delete_write_nothing(
    monkeypatch: pytest.MonkeyPatch, route: str
) -> None:
    signed = Signed(password_customer("+998907770033"))
    original = account_services.lock_customer
    state = {"done": False}

    def delete_then_lock(customer_id: int) -> Customer:
        if not state["done"]:
            state["done"] = True
            account_services.delete_customer(customer_id)
        return original(customer_id)

    monkeypatch.setattr(account_services, "lock_customer", delete_then_lock)
    if route == "address":
        response = signed.client.post(
            ME + "addresses/", {"text": "Chilonzor 9-12, Toshkent"}, **signed.idem()
        )
    elif route == "image":
        response = signed.client.put(ME + "image/", {"file": png()}, format="multipart")
    elif route == "profile":
        response = signed.client.patch(ME, {"name": "Previous Holder"})
    else:
        body = {"current_password": PASSWORD, "password": NEW_PASSWORD}
        response = signed.client.post(ME + "password/", body, **signed.idem())
    assert (response.status_code, response.data["code"]) == (401, "SESSION_EXPIRED")
    # nothing of the write reached the row (the request's transaction, the in-request delete included,
    # rolled back with the 401; the thread race test covers a committed delete)
    customer = Customer.objects.get(pk=signed.customer.pk)
    assert (customer.name, bool(customer.image)) == ("Malika", False)
    assert account_services.check_customer_password(customer, PASSWORD)
    assert not Address.objects.filter(customer=customer).exists()


@pytest.mark.django_db
def test_a_write_from_a_session_the_reset_revoked_is_refused() -> None:
    signed = Signed(password_customer("+998907770034"))
    token = verification(str(signed.customer.phone), "reset_password")
    services.reset_password(token, password=NEW_PASSWORD, device=ctx(FID_B))
    # the request authenticated before the reset (stale auth cache): the service re-checks the sid
    with pytest.raises(PasswordChanged):
        services.change_password(
            signed.customer.pk,
            current_password=NEW_PASSWORD,
            verification_token=None,
            password="Another-Long!Secret-93",
            sid=str(signed.started.session.pk),
        )
    with pytest.raises(PasswordChanged):
        services.add_address(signed.customer.pk, sid=str(signed.started.session.pk), text="Yunusobod 4-5")
    customer = Customer.objects.get(pk=signed.customer.pk)
    assert account_services.check_customer_password(customer, NEW_PASSWORD)


@pytest.mark.django_db
def test_reactivation_scrubs_what_a_racing_write_left_on_the_deleted_row(
    django_capture_on_commit_callbacks: Any,
) -> None:
    publish_terms()
    customer = password_customer("+998907770035")
    account_services.delete_customer(customer.pk)
    # rows a write racing the deletion could have left behind
    Address.objects.create(customer=customer, text="Previous holder home 1", is_default=True)
    sealed = account_services.seal_apple_refresh("prev-apple-refresh", client_id="uz.glowderma.app")
    CustomerSocialIdentity.objects.create(
        customer=customer,
        provider="apple",
        subject="prev-apple",
        provider_refresh_token=sealed,
        provider_client_id="uz.glowderma.app",
    )
    Customer.objects.filter(pk=customer.pk).update(password="argon2$stale")
    token = verification(str(customer.phone), "register")
    with django_capture_on_commit_callbacks(execute=False):
        signed_in = services.register(
            token,
            password=NEW_PASSWORD,
            name="New Holder",
            terms_version=TERMS,
            language="uz",
            device=ctx(FID_A),
        )
    assert signed_in.reactivated is True
    assert not Address.objects.filter(customer=customer).exists()
    assert not CustomerSocialIdentity.objects.filter(customer=customer).exists()
    assert OutboxMessage.objects.filter(topic="apple.revoke").count() == 1
    stored = Customer.objects.get(pk=customer.pk)
    assert account_services.check_customer_password(stored, NEW_PASSWORD)


# --- addresses re-read under the lock --------------------------------------------------------------------


@pytest.mark.django_db
def test_address_changes_after_a_concurrent_removal_answer_404() -> None:
    signed = Signed(password_customer("+998907770036"))
    address = account_services.add_address(signed.customer.pk, text="Toshkent, Chilonzor 9")
    account_services.remove_address(Address.objects.get(pk=address.pk))  # the concurrent DELETE committed
    sid = str(signed.started.session.pk)
    with pytest.raises(NotFound):
        services.update_address(signed.customer.pk, address.pk, sid=sid, label="Uy")
    with pytest.raises(NotFound):
        services.set_default_address(signed.customer.pk, address.pk, sid=sid)
    with pytest.raises(NotFound):
        services.remove_address(signed.customer.pk, address.pk, sid=sid)


@pytest.mark.django_db
def test_address_routes_still_answer_404_for_a_foreign_id() -> None:
    signed = Signed(password_customer("+998907770037"))
    other = password_customer("+998907770038")
    foreign = account_services.add_address(other.pk, text="Boshqa uy, Sergeli 3")
    assert signed.client.patch(ME + f"addresses/{foreign.pk}/", {"label": "x"}).status_code == 404
    assert signed.client.post(ME + f"addresses/{foreign.pk}/default/").status_code == 404
    assert signed.client.delete(ME + f"addresses/{foreign.pk}/").status_code == 404
    assert Address.objects.filter(pk=foreign.pk).exists()


# --- lock order: no deadlock between the flows ------------------------------------------------------------


@pytest.mark.django_db(transaction=True)
def test_logout_vs_sign_in_on_another_device_does_not_deadlock(monkeypatch: pytest.MonkeyPatch) -> None:
    customer = password_customer("+998901000001")
    started = start_test_session(customer, fid=FID_A)
    original = account_services._revoke_open_customer_sessions
    locked, go = threading.Event(), threading.Event()

    def patched(*args: Any, **kwargs: Any) -> Any:
        result = original(*args, **kwargs)
        if threading.current_thread().name == "logout":
            locked.set()
            go.wait(WAIT)
        return result

    monkeypatch.setattr(account_services, "_revoke_open_customer_sessions", patched)
    out: dict[str, Any] = {}
    first = run("logout", lambda: account_services.logout_customer(started.raw_refresh), out)
    assert locked.wait(WAIT)
    second = run(
        "signin",
        lambda: account_services.start_customer_session(
            customer.pk, FID_B, "ios", "1.0.0", issue_device_key=True
        ),
        out,
    )
    time.sleep(1)
    go.set()
    first.join(WAIT)
    second.join(WAIT)
    assert not errors(out), out
    assert AuthSession.objects.filter(customer=customer, revoked_at__isnull=True).count() == 1


@pytest.mark.django_db(transaction=True)
def test_social_sign_in_vs_unlink_does_not_deadlock(monkeypatch: pytest.MonkeyPatch) -> None:
    customer = password_customer("+998901000002")
    CustomerSocialIdentity.objects.create(customer=customer, provider="google", subject="probe-sub-1")
    original = account_services.touch_identity_sign_in
    touched, go = threading.Event(), threading.Event()

    def patched(*args: Any, **kwargs: Any) -> Any:
        result = original(*args, **kwargs)
        if threading.current_thread().name == "social":
            touched.set()
            go.wait(WAIT)
        return result

    monkeypatch.setattr(account_services, "touch_identity_sign_in", patched)
    out: dict[str, Any] = {}
    first = run(
        "social",
        lambda: services.social_sign_in(
            "google", "fake:probe-sub-1:a@example.com", None, "*", None, device=ctx(FID_A)
        ),
        out,
    )
    assert touched.wait(WAIT)
    second = run(
        "unlink",
        lambda: services.unlink_social(
            customer.pk,
            "google",
            password=PASSWORD,
            verification_token=None,
            sid=account_services.NO_SESSION,
        ),
        out,
    )
    time.sleep(1)
    go.set()
    first.join(WAIT)
    second.join(WAIT)
    assert not errors(out), out
    assert isinstance(out["social"][1], services.SignIn)
    assert not CustomerSocialIdentity.objects.filter(customer=customer).exists()


@pytest.mark.django_db(transaction=True)
def test_social_sign_in_of_an_identity_a_reset_unlinked_gets_the_phone_step(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    customer = password_customer("+998901000015")
    CustomerSocialIdentity.objects.create(customer=customer, provider="google", subject="attacker-sub")
    original = account_services.lock_customer
    paused, go = threading.Event(), threading.Event()

    def pause_then_lock(customer_id: int) -> Customer:
        if threading.current_thread().name == "social" and not paused.is_set():
            paused.set()
            go.wait(WAIT)
        return original(customer_id)

    monkeypatch.setattr(account_services, "lock_customer", pause_then_lock)
    out: dict[str, Any] = {}
    social = run(
        "social",
        lambda: services.social_sign_in(
            "google", "fake:attacker-sub:a@example.com", None, "*", None, device=ctx(FID_A)
        ),
        out,
    )
    assert paused.wait(WAIT)
    token = verification(str(customer.phone), "reset_password")
    reset = services.reset_password(token, password=NEW_PASSWORD, device=ctx(FID_B))  # the owner recovers
    go.set()
    social.join(WAIT)
    assert out["social"][0] == "ok" and isinstance(out["social"][1], services.PhoneStep), out
    active = list(AuthSession.objects.filter(customer=customer, revoked_at__isnull=True))
    assert [session.pk for session in active] == [reset.started.session.pk]


@pytest.mark.django_db(transaction=True)
def test_known_device_login_with_the_old_password_loses_to_a_committed_reset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    customer = password_customer("+998901000003")
    started = start_test_session(customer, fid=FID_A)
    original = services.check_login_password
    checked, go = threading.Event(), threading.Event()

    def patched(customer_: Any, raw: str) -> bool:
        result = original(customer_, raw)
        if threading.current_thread().name == "login":
            checked.set()
            go.wait(WAIT)
        return result

    monkeypatch.setattr(services, "check_login_password", patched)
    out: dict[str, Any] = {}
    login = run(
        "login",
        lambda: services.login(
            str(customer.phone), PASSWORD, None, device=ctx(FID_A), device_key=started.raw_device_key
        ),
        out,
    )
    assert checked.wait(WAIT)
    token = verification(str(customer.phone), "reset_password")
    reset = services.reset_password(token, password=NEW_PASSWORD, device=ctx(FID_B))
    go.set()
    login.join(WAIT)
    assert out["login"][0] == "err" and isinstance(out["login"][1], AuthFailed), out
    assert out["login"][1].get_codes() == "invalid_credentials"
    assert AuthSession.objects.get(pk=reset.started.session.pk).revoked_at is None


@pytest.mark.django_db(transaction=True)
def test_one_reset_token_at_reset_and_delete_me_does_not_deadlock() -> None:
    customer = Customer.objects.create(phone="+998901000020", name="Social only")
    started = start_test_session(customer, fid=FID_A)
    token = verification(str(customer.phone), "reset_password")
    barrier = threading.Barrier(2, timeout=WAIT)
    out: dict[str, Any] = {}

    def reset() -> Any:
        barrier.wait()
        return services.reset_password(token, password=NEW_PASSWORD, device=ctx(FID_B))

    def delete() -> Any:
        barrier.wait()
        return services.delete_account(customer.pk, verification_token=token, sid=str(started.session.pk))

    threads = [run("reset", reset, out), run("delete", delete, out)]
    for thread in threads:
        thread.join(WAIT + 5)
    outcomes = sorted(state for state, _ in out.values())
    assert outcomes == ["err", "ok"], out
    failure = next(value for state, value in out.values() if state == "err")
    assert "deadlock" not in str(failure).lower()
    assert isinstance(failure, (AuthFailed, ValidationError)) or type(failure).__name__ in {
        "VerificationInvalid",
        "PhoneNotRegistered",
    }


# --- social completion -------------------------------------------------------------------------------------


@pytest.mark.django_db
def test_social_completion_refuses_a_password_set_after_the_check(monkeypatch: pytest.MonkeyPatch) -> None:
    publish_terms()
    phone = "+998907770066"
    customer = Customer.objects.create(phone=phone, name="Social only")
    step = services.social_sign_in("google", "fake:toctou:t@gmail.com", None, "*", None, device=ctx(FID_A))
    assert isinstance(step, services.PhoneStep)
    token = verification(phone, "link_social", jti=services.read_social_ticket(step.ticket, fid=FID_A).jti)
    original = account_services.lock_customer_by_phone

    def set_password_then_lock(value: str) -> Customer | None:
        account_services.set_customer_password(Customer.objects.get(pk=customer.pk), PASSWORD)
        return original(value)

    monkeypatch.setattr(account_services, "lock_customer_by_phone", set_password_then_lock)
    with pytest.raises(ValidationError) as error:
        services.complete_social(
            step.ticket, token, password=None, name=None, terms_version=None, language=None, device=ctx(FID_A)
        )
    assert error.value.get_codes() == {"password": ["required"]}
    assert not CustomerSocialIdentity.objects.filter(customer=customer).exists()
    # the ticket and the token stay usable for the retry with the password
    monkeypatch.setattr(account_services, "lock_customer_by_phone", original)
    done = services.complete_social(
        step.ticket, token, password=PASSWORD, name=None, terms_version=None, language=None, device=ctx(FID_A)
    )
    assert done.customer_id == customer.pk


class FailingCache:
    """The Django cache, except that ``delete`` of keys with ``prefix`` raises (Redis down)."""

    def __init__(self, prefix: str) -> None:
        self.prefix = prefix

    def __getattr__(self, name: str) -> Any:
        return getattr(cache, name)

    def delete(self, key: str, *args: Any, **kwargs: Any) -> Any:
        if key.startswith(self.prefix):
            raise ConnectionError("redis down")
        return cache.delete(key, *args, **kwargs)


@pytest.mark.django_db
def test_social_completion_survives_a_redis_failure_after_commit(monkeypatch: pytest.MonkeyPatch) -> None:
    publish_terms()
    phone = "+998901000004"
    step = services.social_sign_in(
        "apple",
        "fake-apple:probe-apple-1:x@privaterelay.appleid.com",
        None,
        "*",
        "fake-code:1",
        device=ctx(FID_A),
    )
    assert isinstance(step, services.PhoneStep)
    token = verification(phone, "link_social", jti=services.read_social_ticket(step.ticket, fid=FID_A).jti)
    monkeypatch.setattr(services, "cache", FailingCache("gd:social:apple:"))
    result = services.complete_social(
        step.ticket,
        token,
        password=None,
        name="Ali Valiyev",
        terms_version=TERMS,
        language=None,
        device=ctx(FID_A),
    )
    assert result.started.raw_refresh and result.started.raw_device_key
    assert CustomerSocialIdentity.objects.filter(subject="probe-apple-1").exists()


@pytest.mark.django_db
def test_social_completion_keeps_its_error_when_the_cleanup_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    publish_terms()
    other = password_customer("+998901000106")
    CustomerSocialIdentity.objects.create(customer=other, provider="google", subject="taken-sub")
    step = services.social_sign_in(
        "google", "fake:taken-sub-2:t@gmail.com", None, "*", None, device=ctx(FID_A)
    )
    assert isinstance(step, services.PhoneStep)
    token = verification(
        "+998901000107", "link_social", jti=services.read_social_ticket(step.ticket, fid=FID_A).jti
    )
    monkeypatch.setattr(services, "cache", FailingCache("gd:social:ticket:"))
    with pytest.raises(ValidationError):  # the name is missing: the original error, not ConnectionError
        services.complete_social(
            step.ticket,
            token,
            password=None,
            name=None,
            terms_version=TERMS,
            language=None,
            device=ctx(FID_A),
        )


@pytest.mark.django_db(transaction=True)
def test_a_rolled_back_dormant_reset_keeps_the_image_file() -> None:
    phone = "+998901000006"
    customer = Customer.objects.create(phone=phone, name="Old Holder")
    customer.image.save("probe.webp", ContentFile(b"RIFFxxxxWEBP"), save=True)
    image_name = str(customer.image.name)
    other = password_customer("+998901000116")
    CustomerSocialIdentity.objects.create(customer=other, provider="google", subject="taken-sub")
    jti = "ab" * 16
    ticket = signing.TimestampSigner(salt=services.SOCIAL_TICKET_SALT).sign_object(
        {"provider": "google", "subject": "taken-sub", "email": "", "jti": jti, "fid": pii_ref(FID_A)}
    )
    token = verification(phone, "link_social", jti=jti)
    with pytest.raises(Exception, match="bog‘langan|linked"):
        services.complete_social(
            ticket, token, password=None, name=None, terms_version=None, language=None, device=ctx(FID_A)
        )
    customer.refresh_from_db()
    assert customer.image.name == image_name and customer.name == "Old Holder"
    assert default_storage.exists(image_name)
    default_storage.delete(image_name)


@pytest.mark.django_db(transaction=True)
def test_a_committed_dormant_reset_deletes_the_image_file() -> None:
    customer = Customer.objects.create(phone="+998901000008", name="Old Holder")
    customer.image.save("probe2.webp", ContentFile(b"RIFFxxxxWEBP"), save=True)
    image_name = str(customer.image.name)
    from django.db import transaction

    with transaction.atomic():
        services.dormant_reset(customer.pk, lang="ru", last_activity=None, request=None)
    customer.refresh_from_db()
    assert (customer.name, customer.image.name, customer.language) == ("", "", "ru")
    assert not default_storage.exists(image_name)


# --- tombstones are best effort after commit ----------------------------------------------------------------


@pytest.mark.django_db(transaction=True)
def test_a_tombstone_failure_after_a_committed_login_still_returns_the_tokens(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    customer = password_customer("+998901000016")
    old = start_test_session(customer, fid=FID_A)
    token = verification(str(customer.phone), "login_new_device")

    class SetManyFails:
        def __getattr__(self, name: str) -> Any:
            return getattr(cache, name)

        def set_many(self, *args: Any, **kwargs: Any) -> Any:
            raise ConnectionError("redis down")

    monkeypatch.setattr(account_services, "cache", SetManyFails())
    result = services.login(str(customer.phone), PASSWORD, token, device=ctx(FID_B), device_key=None)
    assert isinstance(result, services.SignIn) and result.started.raw_device_key
    assert AuthSession.objects.get(pk=old.session.pk).revoke_reason == "replaced"


# --- small fixes --------------------------------------------------------------------------------------------


@pytest.mark.django_db
def test_login_failure_of_a_deleted_customer_audits_the_actor() -> None:
    customer = password_customer("+998901000017")
    account_services.delete_customer(customer.pk)
    with pytest.raises(AuthFailed):
        services.login(str(customer.phone), PASSWORD, None, device=ctx(FID_A), device_key=None)
    entry = AuditEntry.objects.filter(action="customer.login_failed").latest("id")
    assert entry.actor_id == customer.pk


@pytest.mark.django_db
def test_a_refused_settings_link_revokes_the_exchanged_apple_token() -> None:
    signed = Signed(password_customer("+998901000018"))
    CustomerSocialIdentity.objects.create(customer=signed.customer, provider="apple", subject="already")
    with pytest.raises(Exception, match="bog‘langan|linked"):
        services.link_social(
            signed.customer.pk,
            "apple",
            "fake-apple:another:x@privaterelay.appleid.com",
            None,
            "*",
            "fake-code:1",
            password=PASSWORD,
            verification_token=None,
            fid=FID_A,
            sid=str(signed.started.session.pk),
        )
    message = OutboxMessage.objects.get(topic="apple.revoke")
    assert message.payload["identity_id"] is None and message.payload["sealed_token"]
    opened = account_services.open_apple_refresh(
        message.payload["sealed_token"], client_id=message.payload["client_id"]
    )
    assert opened == "fake-apple-refresh-another"


@pytest.mark.django_db
def test_an_apple_code_of_another_account_links_nothing() -> None:
    client = app_client()
    body = {
        "provider": "apple",
        "token": "fake-apple:apple-sub:x@privaterelay.appleid.com",
        "nonce": "*",
        "authorization_code": "fake-code-sub:someone-else",
    }
    response = client.post("/api/v1/auth/social/", body)
    assert (response.status_code, response.data["code"]) == (400, "social_token_invalid")
    assert fake_apple().exchanged == []
