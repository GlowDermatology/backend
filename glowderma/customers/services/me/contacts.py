"""Hybrid identity: attaching a phone or an e-mail address (customer contract §2.2.10).

The phone gate (409 ``phone_required`` on checkout, pay, booking and the QR) sends a phoneless account here.
A phone or an address of another account is refused (``phone_taken`` / ``email_taken``) — only after a
correct code, so the routes are no oracle of who holds a number: accounts are never merged, the customer
signs in with that phone instead (or registers with it again when its account was deleted), and money,
cashback and the QR stay with the account they belong to. Attaching adds a way in, so it re-authenticates
(:func:`reauthenticate`) and notifies the account (``push.account_security``; a replaced address also gets
an e-mail)."""

from __future__ import annotations

from typing import Any

from django.db import IntegrityError, transaction

from glowderma.accounts import services as account_services
from glowderma.accounts.models import Customer
from glowderma.core import enums
from glowderma.core.api.exceptions import (
    CustomerError,
    EmailTaken,
    PhoneTaken,
)
from glowderma.customers.services.common import identifiers, results, trail
from glowderma.customers.services.me import reauth


def password_signs_in(customer: Customer) -> bool:
    """Whether the password is a way in: it needs a phone or an e-mail to sign in with (C§2.2.8)."""
    return bool(customer.password) and bool(identifiers.customer_identifier(customer))


def _already_set() -> CustomerError:
    return CustomerError("conflict", 409)


def _refuse_taken_phone(customer_id: int, phone: str) -> None:
    """A phone held by any other account answers 409 ``phone_taken`` with ``details.deleted``: an active
    holder → sign in with it; a deleted one (A1 keeps the number with its loyalty account) → register with
    it again, which reactivates that account (``error.phone_taken_deleted``)."""
    holder = (
        Customer.objects.filter(phone=phone).exclude(pk=customer_id).values_list("status", flat=True).first()
    )
    if holder is None:
        return
    if holder == enums.CustomerStatus.DELETED:
        raise PhoneTaken(key="error.phone_taken_deleted", details={"deleted": True})
    raise PhoneTaken(details={"deleted": False})


def _refuse_taken_email(customer_id: int, email: str) -> None:
    if Customer.objects.filter(email=email).exclude(pk=customer_id).exists():
        raise EmailTaken()


def send_attach_phone_code(
    customer_id: int, phone: str, *, device: results.DeviceContext
) -> account_services.CodeIssue:
    """``POST me/phone/code/``: an ``attach_phone`` SMS code for a phoneless account (409 ``conflict`` when it
    has a phone already); every OTP limit of ``auth/send-code/`` applies. Who holds the number is checked
    only after a correct code (:func:`attach_phone`)."""
    customer = Customer.objects.get(pk=customer_id)
    if customer.phone:
        raise _already_set()
    return account_services.send_code(
        phone,
        enums.OtpPurpose.ATTACH_PHONE,
        ip=device.ip,
        fid=device.fid,
        lang=device.lang,
        platform=device.platform,
    )


def send_verify_email_code(
    customer_id: int, email: str, *, device: results.DeviceContext
) -> account_services.CodeIssue:
    """``POST me/email/code/``: a ``verify_email`` code to the address (409 ``conflict`` when it is already
    the account's); who holds the address is checked only after a correct code."""
    customer = Customer.objects.get(pk=customer_id)
    if customer.email == email:
        raise _already_set()
    return account_services.send_code(
        email,
        enums.OtpPurpose.VERIFY_EMAIL,
        ip=device.ip,
        fid=device.fid,
        lang=device.lang,
        platform=device.platform,
    )


def attach_phone(
    customer_id: int,
    phone: str,
    code: str,
    *,
    password: str | None,
    verification_token: str | None,
    sid: str | account_services.NoSession,
    request: Any = None,
) -> None:
    """``POST me/phone/`` (C§2.2.10): re-authentication first (so its refusal never burns the code), then the
    SMS code (``auth/verify-code/``'s engine: a wrong code is counted and committed), then ``phone_taken``,
    then under the signed-in lock the phone is set (a unique-key race answers ``phone_taken``). Audit
    ``customer.phone_attached`` and ``push.account_security`` ``phone_attached``. Never removed afterwards."""
    snapshot = Customer.objects.get(pk=customer_id)
    if snapshot.phone:
        raise _already_set()
    needed = reauth.reauthenticate(
        snapshot, password=password, verification_token=verification_token, sid=sid
    )
    verified = account_services.verify_code(phone, enums.OtpPurpose.ATTACH_PHONE, code)
    _refuse_taken_phone(customer_id, phone)
    with transaction.atomic():
        customer = reauth.lock_reauthenticated(customer_id, sid, snapshot, verification_token, needed)
        if customer.phone:
            raise _already_set()
        account_services.consume_verification(
            verified.token, purpose=enums.OtpPurpose.ATTACH_PHONE, phone=phone
        )
        _refuse_taken_phone(customer_id, phone)
        try:
            account_services.set_customer_phone(customer_id, phone)
        except IntegrityError as exc:
            raise PhoneTaken(details={"deleted": False}) from exc
        trail.notify_account_security(customer_id, enums.AccountSecurityEvent.PHONE_ATTACHED)
        trail.audit_customer("customer.phone_attached", customer, request)


def attach_email(
    customer_id: int,
    email: str,
    code: str,
    *,
    password: str | None,
    verification_token: str | None,
    sid: str | account_services.NoSession,
    request: Any = None,
) -> None:
    """``POST me/email/`` (C§2.2.10): as :func:`attach_phone` for an e-mail address (a new address replaces
    the previous one; ``email_taken`` for another account's). Audit ``customer.email_attached
    {replaced: bool}``, ``push.account_security`` ``email_attached`` and, for a replacement, a notice e-mail
    to the previous address."""
    snapshot = Customer.objects.get(pk=customer_id)
    if snapshot.email == email:
        raise _already_set()
    needed = reauth.reauthenticate(
        snapshot, password=password, verification_token=verification_token, sid=sid
    )
    verified = account_services.verify_code(email, enums.OtpPurpose.VERIFY_EMAIL, code)
    _refuse_taken_email(customer_id, email)
    with transaction.atomic():
        customer = reauth.lock_reauthenticated(customer_id, sid, snapshot, verification_token, needed)
        account_services.consume_verification(
            verified.token, purpose=enums.OtpPurpose.VERIFY_EMAIL, email=email
        )
        _refuse_taken_email(customer_id, email)
        try:
            account_services.set_customer_email(customer_id, email)
        except IntegrityError as exc:
            raise EmailTaken() from exc
        trail.notify_account_security(customer_id, enums.AccountSecurityEvent.EMAIL_ATTACHED)
        if customer.email:
            account_services.enqueue_email_notice(
                customer.email, lang=customer.language, kind="email_replaced"
            )
        trail.audit_customer("customer.email_attached", customer, request, {"replaced": bool(customer.email)})


__all__ = [
    "attach_email",
    "attach_phone",
    "password_signs_in",
    "send_attach_phone_code",
    "send_verify_email_code",
]
