"""Customer profile writes the ``customers`` orchestrators make (customer contract §2.2): account creation,
phone / e-mail / language / consent, the profile image, the password hash and the e-mail notices.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from django.contrib.auth.hashers import check_password, make_password
from django.core.files.base import ContentFile
from django.db import transaction
from rest_framework.exceptions import ValidationError

from glowderma.accounts import topics
from glowderma.accounts.models import Customer, Device
from glowderma.accounts.services.otp import targets
from glowderma.core import outbox, secretbox, timeutils
from glowderma.core.api.exceptions import PhoneRequired, field_error
from glowderma.core.variants import delete_with_variants, store_variants


def set_customer_password(customer: Customer, raw_password: str) -> None:
    customer.password = make_password(raw_password)
    customer.password_changed_at = timeutils.now()
    customer.save(update_fields=["password", "password_changed_at"])


def check_customer_password(customer: Customer, raw_password: str) -> bool:
    return bool(customer.password) and check_password(raw_password, customer.password)


def delete_image_on_commit(customer: Customer) -> None:
    """Delete the customer's image file once the transaction that clears the column commits; a rollback
    keeps both the column and the file (a deleted file behind a kept name would break ``image_url``)."""
    if not customer.image:
        return
    storage, name = customer.image.storage, str(customer.image.name)
    transaction.on_commit(lambda: delete_with_variants(storage, name), robust=True)


def move_history_horizon(customer_id: int, *, at: datetime) -> None:
    """Set ``Customer.history_from`` (customer contract §2.2.3): the customer's own history starts there."""
    Customer.objects.filter(pk=customer_id).update(history_from=at, updated_at=timeutils.now())


def create_customer(phone: str | None, *, name: str, language: str, email: str | None = None) -> Customer:
    """A new customer row (registration, social completion): with a verified phone, a verified e-mail
    (``email_verified_at`` = now), both, or neither (a social sign-up, hybrid identity). ``IntegrityError``
    when the phone or the address was taken meanwhile (inside its own savepoint, so the caller's transaction
    stays usable)."""
    address = targets.normalize_email(email) if email else None
    with transaction.atomic():
        return Customer.objects.create(
            phone=phone or None,
            email=address,
            email_verified_at=timeutils.now() if address else None,
            name=name,
            language=language,
        )


def require_phone(customer: Customer) -> None:
    """The phone gate (hybrid identity, customer contract §2.2.10): checkout, pay, booking and the QR answer
    409 ``phone_required`` until the account has a verified phone (``me/phone/``). Every stored phone was
    verified by an SMS code (registration, social completion or attach), and a phone is never removed."""
    if not customer.phone:
        raise PhoneRequired()


def set_customer_phone(customer_id: int, phone: str) -> None:
    """Attach a verified phone to a phoneless account (hybrid identity, customer contract §2.2.10), under the
    caller's customer-level lock; ``IntegrityError`` (own savepoint) when another row holds it."""
    with transaction.atomic():
        Customer.objects.filter(pk=customer_id, phone__isnull=True).update(
            phone=phone, updated_at=timeutils.now()
        )


def set_customer_email(customer_id: int, email: str) -> None:
    """Set the verified e-mail address (replacing a previous one), ``email_verified_at`` = now, under the
    caller's customer-level lock; ``IntegrityError`` (own savepoint) when another row holds it."""
    instant = timeutils.now()
    with transaction.atomic():
        Customer.objects.filter(pk=customer_id).update(
            email=targets.normalize_email(email), email_verified_at=instant, updated_at=instant
        )


EMAIL_NOTICE_AAD = b"email.notice"
EMAIL_NOTICE_KINDS = frozenset({"email_replaced"})


def enqueue_email_notice(address: str, *, lang: str, kind: str) -> None:
    """A security notice e-mail to ``address`` (hybrid identity: the previous address when it is replaced,
    C§2.2.10) through the outbox topic ``email.notice``; the address travels sealed (``core.secretbox``),
    never in clear in the payload."""
    if kind not in EMAIL_NOTICE_KINDS:
        raise ValueError(f"Unknown e-mail notice {kind!r}")
    outbox.enqueue(
        topics.EMAIL_NOTICE,
        {"to": secretbox.seal(address.encode("utf-8"), aad=EMAIL_NOTICE_AAD), "lang": lang, "kind": kind},
        dedupe_key=f"{topics.EMAIL_NOTICE}:{uuid.uuid4().hex}",
    )


def set_customer_language(customer_id: int, language: str) -> None:
    Customer.objects.filter(pk=customer_id).update(language=language, updated_at=timeutils.now())


def stamp_consent(customer_id: int, *, terms_version: str, ip: str) -> None:
    """Accept the terms (customer contract §2.1.3, §2.2.4): version, instant and client IP."""
    instant = timeutils.now()
    Customer.objects.filter(pk=customer_id).update(
        terms_version=terms_version, consent_at=instant, consent_ip=ip or None, updated_at=instant
    )


_KEEP: Any = object()


def update_customer_profile(
    customer_id: int, *, fid: str, name: str = _KEEP, language: str = _KEEP, push_enabled: bool = _KEEP
) -> None:
    """``PATCH me/``: name and language on the customer, ``push_enabled`` on the current device (field
    ``push_enabled`` ``push_token_missing`` without a stored FCM token)."""
    if push_enabled is not _KEEP:
        device = Device.objects.select_for_update().get(customer_id=customer_id, fid=fid)
        if push_enabled and not device.fcm_token:
            raise ValidationError(
                {"push_enabled": [field_error("push_token_missing", message_key="field.push_token_missing")]}
            )
        device.push_enabled = bool(push_enabled)
        device.save(update_fields=["push_enabled", "updated_at"])
    fields = {key: value for key, value in (("name", name), ("language", language)) if value is not _KEEP}
    if fields:
        Customer.objects.filter(pk=customer_id).update(**fields, updated_at=timeutils.now())


def set_customer_image(customer: Customer, filename: str, content: bytes) -> str:
    """``PUT me/image/`` on the locked ``customer``: store the processed file, delete the previous one on
    commit. Returns the new storage name."""
    previous = str(customer.image.name) if customer.image else ""
    storage = customer.image.storage
    customer.image.save(filename, ContentFile(content), save=True)
    new_name = str(customer.image.name)
    try:
        store_variants(storage, new_name, content)
    except BaseException:
        storage.delete(new_name)  # the row rolls back: no orphan original
        raise
    if previous and previous != new_name:
        transaction.on_commit(lambda: delete_with_variants(storage, previous), robust=True)
    return new_name


def clear_customer_image(customer_id: int) -> None:
    """``DELETE me/image/``: the column cleared now, the file deleted on commit."""
    stored = Customer.objects.get(pk=customer_id)
    if stored.image:
        delete_image_on_commit(stored)
        Customer.objects.filter(pk=customer_id).update(image="", updated_at=timeutils.now())


__all__ = [
    "set_customer_password",
    "check_customer_password",
    "move_history_horizon",
    "create_customer",
    "require_phone",
    "set_customer_phone",
    "set_customer_email",
    "EMAIL_NOTICE_AAD",
    "EMAIL_NOTICE_KINDS",
    "enqueue_email_notice",
    "set_customer_language",
    "stamp_consent",
    "update_customer_profile",
    "set_customer_image",
    "clear_customer_image",
    "delete_image_on_commit",
]
