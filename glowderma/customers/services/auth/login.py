"""Customer password login (customer contract §2.1.4): a known device signs in, a new one gets a code."""

from __future__ import annotations

from typing import Any

from django.db import transaction

from glowderma.accounts import services as account_services
from glowderma.accounts.models import Customer, Device
from glowderma.accounts.security.login_lockout import LoginGuard
from glowderma.core import enums, timeutils
from glowderma.core.api.exceptions import (
    AuthFailed,
    VerificationInvalid,
)
from glowderma.customers.services.common import identifiers, passwords, results, trail

# Where a new-device code can go (``otp_channel``): the account's verified phone (SMS) or e-mail address.
SMS_CHANNEL = enums.OtpChannel.SMS.value
EMAIL_CHANNEL = enums.OtpChannel.EMAIL.value
OTP_CHANNELS = (SMS_CHANNEL, EMAIL_CHANNEL)


def code_channels(customer: Customer | None) -> list[tuple[str, str]]:
    """``(channel, phone or address)`` of every place the account's new-device code can be sent: its phone
    (always verified) and its verified e-mail address (an e-mail is stored only once verified)."""
    if customer is None:
        return []
    found = []
    if customer.phone:
        found.append((SMS_CHANNEL, customer.phone))
    if customer.email and customer.email_verified_at is not None:
        found.append((EMAIL_CHANNEL, customer.email))
    return found


def code_target(identifier: str, channel: str | None, customer: Customer | None = None) -> str | None:
    """Where the new-device code of a login with ``identifier`` goes for ``channel`` (``None`` = where the
    customer signed in with): the account's identifier on that channel, or ``None`` when the account has none
    (an unknown identifier keeps its own target, so the answer reveals nothing)."""
    if channel is None:
        return identifier
    account = customer if customer is not None else account_services.active_customer_by_phone(identifier)
    if account is None:
        return identifier
    return dict(code_channels(account)).get(channel)


def login(
    identifier: str,
    password: str,
    verification_token: str | None,
    *,
    device: results.DeviceContext,
    device_key: str | None,
    request: Any = None,
    otp_channel: str | None = None,
) -> results.SignIn | results.DeviceStep:
    """``auth/login/`` steps 1–7 (customer contract §2.1.4) with a phone or an e-mail address (hybrid
    identity: the new-device code goes where the customer signed in with, or — ``otp_channel`` — to the
    account's other verified identifier; a channel the account lacks is refused after the password check,
    field ``otp_channel`` ``invalid``)."""
    customer = account_services.active_customer_by_phone(identifier)
    known = customer is not None and account_services.device_is_known(customer.pk, device.fid, device_key)
    guard = LoginGuard()
    if known and customer is not None:
        device_id = Device.objects.filter(customer_id=customer.pk, fid=device.fid).values_list(
            "pk", flat=True
        )[0]
        scope, key = identifiers.LOGIN_DEVICE_SCOPE, str(device_id)
    else:
        scope, key = identifiers.LOGIN_SCOPE, identifiers.phone_guard_key(identifier)
    # The unknown-device budget is per identifier and, once it resolves to an account, per account too: an
    # account with a phone and an e-mail gets one budget, not two. An unknown identifier keeps its own key
    # only, so the answer reveals nothing (the known-device scope stays per device, C§1.8).
    keys = [(scope, key)]
    if scope == identifiers.LOGIN_SCOPE and customer is not None:
        keys.append((identifiers.LOGIN_SCOPE, identifiers.customer_guard_key(customer.pk)))
    for guard_scope, guard_key in keys:
        guard.check(guard_scope, guard_key)
    with transaction.atomic():
        target = code_target(identifier, otp_channel, customer)
        if not known and not verification_token:
            block = account_services.otp_blocked(
                target or identifier, fid=device.fid, ip=device.ip, now=timeutils.now()
            )
            if block is not None:
                raise block
        ok = passwords.check_login_password(customer, password)
        if ok and customer is not None:
            ok = _login_still_valid(customer, device=device, device_key=device_key, known=known)
        if ok and customer is not None:
            for guard_scope, guard_key in keys:
                guard.reset(guard_scope, guard_key)
            if known:
                started = account_services.start_customer_session(
                    customer.pk, device.fid, device.platform, device.app_version, issue_device_key=False
                )
                trail.audit_customer(
                    "customer.login_succeeded", customer, request, {"method": "password", "new_device": False}
                )
                return results.SignIn(started, customer.pk)
            if target is None:
                raise passwords.field_refusal("otp_channel", "invalid")
            if not verification_token:
                issue = account_services.issue_login_code(
                    target, ip=device.ip, fid=device.fid, lang=device.lang, platform=device.platform
                )
                return results.DeviceStep(issue, channels=code_channels(customer))
            _consume_login_token(verification_token, customer, preferred=target)
            started = account_services.start_customer_session(
                customer.pk, device.fid, device.platform, device.app_version, issue_device_key=True
            )
            trail.audit_customer(
                "customer.login_succeeded", customer, request, {"method": "password", "new_device": True}
            )
            trail.alert_by_email(customer.pk, "new_sign_in")
            return results.SignIn(started, customer.pk)
    for guard_scope, guard_key in keys:
        guard.record_failure(guard_scope, guard_key)
    # actor_id whenever the customer row exists, deleted included (C§2.1.4 step 4)
    actor = customer or Customer.objects.filter(**identifiers.target_kwargs(identifier)).first()
    trail.audit_customer("customer.login_failed", actor, request, {"method": "password"})
    raise AuthFailed("invalid_credentials")


def _consume_login_token(token: str, customer: Customer, *, preferred: str) -> None:
    """Use the ``login_new_device`` verification token, issued for whichever of the account's identifiers the
    code went to (the one signed in with, or the ``otp_channel`` one: a client that switched channels may not
    repeat ``otp_channel`` on this call). The preferred target is tried first."""
    candidates = [preferred] + [value for _channel, value in code_channels(customer) if value != preferred]
    for value in candidates[:-1]:
        try:
            with transaction.atomic():
                account_services.consume_verification(
                    token, purpose=enums.OtpPurpose.LOGIN_NEW_DEVICE, **identifiers.target_kwargs(value)
                )
            return
        except VerificationInvalid:
            continue  # issued for another of the account's identifiers
    account_services.consume_verification(
        token, purpose=enums.OtpPurpose.LOGIN_NEW_DEVICE, **identifiers.target_kwargs(candidates[-1])
    )


def _login_still_valid(
    customer: Customer, *, device: results.DeviceContext, device_key: str | None, known: bool
) -> bool:
    """Re-check the unlocked snapshot under the customer-level lock (C§2.7.1): still active, the hash just
    checked is still the stored one (a reset committing meanwhile wins), and for a known device its key
    still holds (a reset clears the other devices' keys)."""
    locked = account_services.lock_customer(customer.pk)
    if locked.status != enums.CustomerStatus.ACTIVE or locked.password != customer.password:
        return False
    return not known or account_services.device_is_known(customer.pk, device.fid, device_key)


__all__ = [
    "EMAIL_CHANNEL",
    "OTP_CHANNELS",
    "SMS_CHANNEL",
    "code_channels",
    "code_target",
    "login",
]
