"""Shared helpers of the P1 customer API tests (auth, profile, catalog)."""

from __future__ import annotations

import secrets
from datetime import timedelta
from typing import Any

from django.core.cache import cache
from rest_framework.test import APIClient

from glowderma.accounts import services as account_services
from glowderma.accounts.models import Customer, SmsCode
from glowderma.accounts.tests.factories import device_headers
from glowderma.core import timeutils
from glowderma.integrations.eskiz.registry import fake_eskiz
from glowderma.integrations.oauth.registry import fake_apple
from glowderma.site_config.models import SiteSettings

TERMS = "2026-10-01"
PASSWORD = "Saffron!Harbor-Long72"  # noqa: S105 - test-only
NEW_PASSWORD = "Juniper-Ocean!Bright58"  # noqa: S105 - test-only
FID = "fid-p1-device-0001"
OTHER_FID = "fid-p1-device-0002"


def publish_terms(version: str = TERMS) -> SiteSettings:
    row = SiteSettings.objects.get(pk=1)
    row.terms_version = version
    row.terms_uz = "Foydalanish shartlari"
    row.privacy_policy_uz = "Maxfiylik siyosati"
    row.terms_updated_at = timeutils.now()
    row.save()
    return row


def app_client(fid: str = FID, platform: str = "ios", key: str | None = None, **extra: Any) -> APIClient:
    client = APIClient()
    client.default_format = "json"
    client.credentials(**device_headers(fid=fid, platform=platform, key=key), **extra)
    return client


def bearer(client: APIClient, access: str, fid: str = FID, key: str | None = None) -> APIClient:
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {access}", **device_headers(fid=fid, key=key))
    return client


def verification(phone: str, purpose: str, *, jti: str | None = None, minutes: int = 10) -> str:
    """A fresh, unused verification token of ``purpose`` for ``phone`` (as ``verify-code`` would issue)."""
    token = secrets.token_urlsafe(32)
    now = timeutils.now()
    row = SmsCode.objects.create(
        phone=phone,
        purpose=account_services.OTP_PURPOSES[purpose],
        code_hmac="0" * 64,
        created_at=now,
        expires_at=now,
        consumed_at=now,
        verification_token_hash=account_services.hash_secret(token),
        verification_expires_at=now + timedelta(minutes=minutes),
    )
    if jti is not None:
        cache.set(account_services.social_otp_key(row.pk), jti, 900)
    return token


def sent_codes() -> list[str]:
    """The codes the fake Eskiz received (the last six characters of the first line)."""
    return [message.text.split("\n")[0].split("kodingiz ")[-1][:6] for message in fake_eskiz().sent]


def reset_fakes() -> None:
    fake_eskiz().sent.clear()
    fake_apple().revoked.clear()
    fake_apple().exchanged.clear()


def password_customer(phone: str = "+998901112233", **fields: Any) -> Customer:
    customer = Customer.objects.create(phone=phone, name=fields.pop("name", "Malika"), **fields)
    account_services.set_customer_password(customer, PASSWORD)
    return customer
