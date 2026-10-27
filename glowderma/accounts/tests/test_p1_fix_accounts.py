"""Fix pass 1 regressions of the accounts P1 block and handlers: sms.send reads before it claims, apple.revoke
never trusts the fake under the production settings, logout takes the customer lock first, the re-rotation's
dead-hash entry rolls back, the OTP IP counter is pseudonymous, the image file goes only on commit."""

from __future__ import annotations

import logging
from typing import Any

import pytest
from django.core.cache import cache
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from django.db import transaction
from django.test import override_settings

from glowderma.accounts import services, tasks
from glowderma.accounts.models import Customer, SmsCode
from glowderma.accounts.tests.factories import start_test_session
from glowderma.core import outbox
from glowderma.core.api.exceptions import AuthFailed
from glowderma.core.models import OutboxMessage
from glowderma.integrations.eskiz import registry as eskiz_registry
from glowderma.integrations.oauth.fake import FakeApple
from glowderma.integrations.oauth.registry import fake_apple

PHONE = "+998901231299"
FID = "fid-fix-accounts-01"


@pytest.fixture(autouse=True)
def clean_fakes() -> Any:
    eskiz_registry.fake_eskiz().sent.clear()
    fake_apple().revoked.clear()
    yield
    eskiz_registry.fake_eskiz().sent.clear()
    fake_apple().revoked.clear()


def issue(phone: str = PHONE) -> OutboxMessage:
    services.send_code(phone, "register", ip="203.0.113.7", fid=FID, lang="uz", platform="ios")
    row = SmsCode.objects.get(phone=phone)
    return OutboxMessage.objects.get(dedupe_key=f"sms.send:{row.pk}")


class GetFails:
    def __getattr__(self, name: str) -> Any:
        return getattr(cache, name)

    def get(self, *args: Any, **kwargs: Any) -> Any:
        raise ConnectionError("redis down")


@pytest.mark.django_db
def test_sms_send_reads_the_payload_before_claiming(monkeypatch: pytest.MonkeyPatch) -> None:
    message = issue()
    monkeypatch.setattr(tasks, "cache", GetFails())
    with pytest.raises(ConnectionError):
        tasks.send_sms(message)
    assert SmsCode.objects.get(phone=PHONE).sent_at is None  # nothing claimed
    monkeypatch.setattr(tasks, "cache", cache)
    tasks.send_sms(message)  # the outbox retry sends
    assert len(eskiz_registry.fake_eskiz().sent) == 1
    assert SmsCode.objects.get(phone=PHONE).sent_at is not None


@pytest.mark.django_db
def test_sms_send_survives_a_payload_delete_failure_after_sending(monkeypatch: pytest.MonkeyPatch) -> None:
    message = issue()

    class DeleteFails:
        def __getattr__(self, name: str) -> Any:
            return getattr(cache, name)

        def delete(self, *args: Any, **kwargs: Any) -> Any:
            raise ConnectionError("redis down")

    monkeypatch.setattr(tasks, "cache", DeleteFails())
    tasks.send_sms(message)  # no exception: the SMS went out, the outbox must not retry
    assert len(eskiz_registry.fake_eskiz().sent) == 1


def revoke_message(identity_id: int, token: str, client_id: str = "uz.glowderma.app") -> OutboxMessage:
    return outbox.enqueue(
        "apple.revoke",
        {
            "identity_id": identity_id,
            "client_id": client_id,
            "sealed_token": services.seal_apple_refresh(token, client_id=client_id),
        },
        dedupe_key=f"apple.revoke:{identity_id}",
    )


@pytest.mark.django_db
@override_settings(DEV_FEATURES_ALLOWED=False, OAUTH_MODE="fake")
def test_apple_revoke_is_never_reported_done_through_the_fake_in_production(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    calls: list[str] = []
    monkeypatch.setattr(FakeApple, "revoke", lambda self, token, *, client_id: calls.append(token))
    message = revoke_message(21, "real-apple-refresh")
    with caplog.at_level(logging.ERROR), pytest.raises(tasks.AppleRevokeNotSent):
        tasks.revoke_apple_token(message)
    assert calls == []
    message.refresh_from_db()
    assert message.payload["sealed_token"]  # kept for a later live revocation or staff
    assert any("apple.revoke_not_sent identity_id=21" in record.getMessage() for record in caplog.records)


@pytest.mark.django_db
def test_apple_revoke_without_identity_is_revoked() -> None:
    sealed = services.seal_apple_refresh("orphan-refresh", client_id="uz.glowderma.app")
    with transaction.atomic():
        services.enqueue_apple_token_revoke(
            sealed, client_id="uz.glowderma.app", customer_id=None, subject="orphan-sub"
        )
    message = OutboxMessage.objects.get(topic="apple.revoke")
    tasks.revoke_apple_token(message)
    assert fake_apple().revoked == [("orphan-refresh", "uz.glowderma.app")]


@pytest.mark.django_db
def test_logout_refuses_revoked_and_deleted_sessions() -> None:
    customer = Customer.objects.create(phone=PHONE, name="Malika")
    started = start_test_session(customer, fid=FID)
    services.logout_customer(started.raw_refresh)
    with pytest.raises(AuthFailed):
        services.logout_customer(started.raw_refresh)
    again = start_test_session(customer, fid=FID)
    services.delete_customer(customer.pk)
    with pytest.raises(AuthFailed):
        services.logout_customer(again.raw_refresh)
    with pytest.raises(AuthFailed):
        services.logout_customer("unknown-refresh-token")


@pytest.mark.django_db
def test_a_rolled_back_re_rotation_forgets_the_dead_hash(monkeypatch: pytest.MonkeyPatch) -> None:
    customer = Customer.objects.create(phone=PHONE, name="Malika")
    started = start_test_session(customer, fid=FID)
    first = services.rotate_customer_refresh(started.raw_refresh, fid=FID)
    current_hash = services.hash_secret(first.body["refresh"])
    cache.delete(services.REFRESH_GRACE_KEY.format(digest=services.hash_secret(started.raw_refresh)))

    def boom(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("token signing failed")

    monkeypatch.setattr(services, "refresh_body", boom)
    with pytest.raises(RuntimeError):
        services.rotate_customer_refresh(started.raw_refresh, fid=FID)  # the re-rotation rolls back
    assert cache.get(services.REFRESH_DEAD_KEY.format(digest=current_hash)) is None
    monkeypatch.undo()
    # the still-current token keeps working instead of counting as reuse
    assert services.rotate_customer_refresh(first.body["refresh"], fid=FID).body["refresh"]


@pytest.mark.django_db
def test_the_otp_ip_counter_key_holds_no_raw_ip() -> None:
    issue()
    keys = [key for key in getattr(cache, "_cache", {}) if "gd:otp:ip:" in str(key)]
    assert keys and not any("203.0.113.7" in str(key) for key in keys)


@pytest.mark.django_db
def test_otp_issued_logs_the_contract_purpose(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.INFO, logger="glowderma.accounts.services"):
        issue()
    messages = [record.getMessage() for record in caplog.records if "otp.issued" in record.getMessage()]
    assert messages and "purpose=register " in messages[0]


@pytest.mark.django_db(transaction=True)
def test_delete_customer_deletes_the_image_file_only_on_commit() -> None:
    customer = Customer.objects.create(phone=PHONE, name="Malika")
    customer.image.save("keep.webp", ContentFile(b"RIFFxxxxWEBP"), save=True)
    name = str(customer.image.name)
    with pytest.raises(RuntimeError), transaction.atomic():
        services.delete_customer(customer.pk)
        raise RuntimeError("a later step refused")
    assert default_storage.exists(name)
    services.delete_customer(customer.pk)
    assert not default_storage.exists(name)


@pytest.mark.django_db(transaction=True)
def test_a_tombstone_failure_is_logged_not_raised(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    customer = Customer.objects.create(phone=PHONE, name="Malika")
    started = start_test_session(customer, fid=FID)

    class SetManyFails:
        def __getattr__(self, name: str) -> Any:
            return getattr(cache, name)

        def set_many(self, *args: Any, **kwargs: Any) -> Any:
            raise ConnectionError("redis down")

    monkeypatch.setattr(services, "cache", SetManyFails())
    with caplog.at_level(logging.ERROR):
        services.logout_customer(started.raw_refresh)
    assert any("auth.tombstone_failed" in record.getMessage() for record in caplog.records)
